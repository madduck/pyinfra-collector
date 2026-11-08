"""Collect facts across the entire inventory before a deploy continues.

The module-level :func:`collect` gathers one explicit request through pyinfra's
fact pool and returns a mapping of host names to values without using storage.
Equivalent calls in one state share a collection, including its failure. They do
not wait for other hosts to load their deploy code.

:class:`FactCollector` also supports explicit request queues for Python callers
that manage their own collection boundary.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from copy import copy, deepcopy
from dataclasses import dataclass, field
from gettext import gettext as _
from sys import exc_info
from typing import Any, cast

from gevent import joinall
from gevent.event import AsyncResult
from gevent.lock import Semaphore
from paramiko import SSHException
from pyinfra import logger
from pyinfra.api.exceptions import ConnectError, PyinfraError
from pyinfra.api.host import Host
from pyinfra.api.state import State
from pyinfra.context import ctx_host, ctx_state

from .typedefs import FactClass, args_data, fact_id

#: Selects hosts from the entire inventory before connection work starts.
#: Predicates can inspect local metadata but must not read remote facts.
HostPredicate = Callable[[Host, FactClass], bool]


def _always(_host: Host, _fact: FactClass) -> bool:
    """Include every host."""
    return True


@dataclass
class FactRequest:
    """A request to gather one fact.

    Attributes:
        fact: The fact class to gather.
        args: Positional arguments to pass to the fact's command.
        kwargs: Keyword arguments to pass to the fact's command.
        include: Predicate deciding which hosts contribute.
    """

    fact: FactClass
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)
    include: HostPredicate = _always

    @property
    def id(self) -> str:
        """Return the canonical fact id."""
        return fact_id(self.fact)

    @property
    def args_data(self) -> dict[str, Any]:
        """Return the bound arguments as JSON-compatible data for storage."""
        return args_data(self.args, self.kwargs)

    @property
    def key(self) -> tuple[str, str]:
        """Return the fact and argument key identifying this request."""
        return (self.id, json.dumps(self.args_data, sort_keys=True, default=str))


class FactCollector:
    """Accumulate requests and gather them at an explicit collection boundary.

    Adding a request does not contact hosts. The caller must bind the pyinfra
    state and call :meth:`run` after adding all requests.
    """

    def __init__(self) -> None:
        """Create an empty collector without opening a store."""
        self._pending: dict[tuple[str, str], FactRequest] = {}

    def gather(
        self,
        fact: FactClass,
        *args: Any,
        include: HostPredicate = _always,
        **kwargs: Any,
    ) -> None:
        """Add a fact to gather without contacting hosts.

        Adding the same fact with the same arguments twice is a no-op. The
        first request's predicate applies.

        Args:
            fact: The fact class to gather.
            *args: Positional arguments for the fact's command.
            include: Predicate deciding which hosts contribute.
            **kwargs: Keyword arguments for the fact's command.
        """
        request = FactRequest(fact=fact, args=args, kwargs=kwargs, include=include)
        self._pending.setdefault(request.key, request)

    @property
    def pending(self) -> list[FactRequest]:
        """Return the pending requests in the order they were added."""
        return list(self._pending.values())

    def run(self) -> list[tuple[FactRequest, dict[str, Any]]]:
        """Gather every pending fact without reading or writing storage.

        Selected inventory hosts are gathered through ``state.fact_pool``,
        regardless of deployment limits. Existing connections are reused.
        Collection-only connections close after use. Unavailable hosts are
        omitted. A heading is logged once per state, and each successful result
        logs its short fact name with the host prefix as it arrives. A blank
        line follows each completed request. Requests
        remain pending until :meth:`reset` is called.

        Returns:
            Request/result pairs in queue order. Each result maps host names
            to collected values. The request retains the fact and arguments
            for callers that choose to persist its result.
        """
        return [(request, self._collect(request)) for request in self.pending]

    def _collect(self, request: FactRequest) -> dict[str, Any]:
        """Gather one request and return its values without using storage."""
        state = cast(State, ctx_state.get())  # type: ignore[no-untyped-call]
        hosts = list(state.inventory)
        if not vars(state).get("_pyinfra_collector_logging_started", False):
            # Mark before logging or collection can yield to another request.
            vars(state)["_pyinfra_collector_logging_started"] = True
            logger.info(_("Collecting facts from hosts..."))
        values = {host.name: data for host, data in self._gather(state, request, hosts)}
        logger.info("")
        return values

    def _gather(
        self,
        state: State,
        request: FactRequest,
        hosts: list[Host],
    ) -> list[tuple[Host, Any]]:
        """Collect one fact from selected hosts through the existing fact pool.

        Args:
            state: The current state, providing the fact pool.
            request: The fact to collect.
            hosts: All inventory hosts, including non-deployment targets.

        Returns:
            Successful host/value pairs in inventory order. Connection and
            native fact failures are omitted without changing deploy targets.
        """
        selected = []
        for host in hosts:
            with ctx_state.use(state), ctx_host.use(host):
                if request.include(host, request.fact):
                    selected.append(host)

        def gather_one(host: Host) -> tuple[Host, Any] | None:
            # Different requests can collect from the same host concurrently.
            # Serialise access to that connector, including connection cleanup.
            lock = vars(host).setdefault("_pyinfra_collector_lock", Semaphore())
            with lock, ctx_state.use(state), ctx_host.use(host):
                already_connected = host.connected
                try:
                    host.connect(raise_exceptions=True)
                    # Pyinfra's fact API marks hosts failed on command errors.
                    # Keep that bookkeeping local, without replacing the real
                    # connector or changing the deploy's active/failed hosts.
                    fact_host = copy(host)
                    fact_state = copy(state)
                    fact_host.state = fact_state
                    fact_state.active_hosts = {fact_host}
                    fact_state.activated_hosts = {fact_host}
                    fact_state.failed_hosts = set()
                    with ctx_state.use(fact_state), ctx_host.use(fact_host):
                        try:
                            data = fact_host.get_fact(
                                request.fact,
                                *request.args,
                                **(request.kwargs | {"_ignore_errors": False}),
                            )
                        except PyinfraError:
                            if fact_host not in fact_state.failed_hosts:
                                raise
                            return None
                    if fact_host in fact_state.failed_hosts:
                        return None
                    host.log(request.fact.__name__)
                    return host, data
                except (ConnectError, OSError, SSHException):
                    return None
                finally:
                    if not already_connected and host.connected:
                        host.disconnect()

        results = [state.fact_pool.spawn(gather_one, host) for host in selected]
        # Drain all workers before returning or raising, including their
        # connection cleanup when another host's fact raises unexpectedly.
        joinall(results)
        return [
            result for greenlet in results if (result := greenlet.get()) is not None
        ]

    def reset(self) -> None:
        """Forget all pending requests."""
        self._pending.clear()


@dataclass
class _Collection:
    """Retain a request and its shared result for the lifetime of one state."""

    # Retaining the predicate prevents its identity from being reused while the
    # cache entry exists. Different predicates must not share filtered results.
    request: FactRequest
    result: AsyncResult[dict[str, Any]] = field(default_factory=AsyncResult)


def collect(
    fact: FactClass,
    *args: Any,
    include: HostPredicate = _always,
    **kwargs: Any,
) -> dict[str, Any]:
    """Collect one fact across the inventory without reading or writing storage.

    Equivalent calls in one pyinfra state share one collection. Equivalence
    requires the same fact id, arguments, and predicate object. The first caller
    collects through ``state.fact_pool``, reusing existing connections and
    connecting selected hosts as needed. Collection-only connections close
    after use. Other callers wait for this result, not for hosts to load.
    Collection therefore works with capped ``PARALLEL`` and in a deploy that
    only some hosts execute. ``PARALLEL=1`` permits only serial collection.

    Results contain only hosts selected for this request. Connection and native
    fact failures omit the affected host. Deployment targets and their failure
    state do not change. Every caller receives an independent copy after
    collection completes. A collection heading is logged once per state. Each
    successful result logs its short fact name with the host prefix as it
    arrives, including at normal verbosity. Reusing a shared result does not
    repeat these messages. A blank line follows each completed request.
    Unexpected errors raise for every caller without
    retrying in the same state. Persistence and missing-value policy belong
    to the caller.

    Args:
        fact: The fact class to collect.
        *args: Positional arguments for the fact's command.
        include: Predicate applied to every inventory host before fan-out and
            connection work. It must use local metadata, not remote facts or a
            connection. Reuse the same callable object for calls that must share
            a filtered collection.
        **kwargs: Keyword arguments for the fact's command.

    Returns:
        A mapping of selected, successful inventory host names to values.
        Deployment limits such as ``--limit`` do not restrict collection.
        Hosts rejected by the predicate or unavailable during collection are
        absent. A successful ``None`` value is retained.

    Raises:
        PyinfraError: If there is no active pyinfra state.
        BaseException: If an unexpected collection error occurs.
    """
    state = cast(State | None, ctx_state.get())  # type: ignore[no-untyped-call]
    if state is None:
        raise PyinfraError(_("collect() requires an active pyinfra state"))

    # Keep ownership on the state. A global weak-key cache would still retain
    # failed states through exception tracebacks stored in its values.
    collections = cast(
        dict[tuple[str, str, int], _Collection],
        vars(state).setdefault("_pyinfra_collector_collections", {}),
    )

    request = FactRequest(fact=fact, args=args, kwargs=kwargs, include=include)
    key = (*request.key, id(include))
    try:
        collection = collections[key]
    except KeyError:
        collection = _Collection(request)
        # Publish before gathering can yield, so concurrent callers wait on the
        # same result even when the fact pool has fewer slots than hosts.
        collections[key] = collection
        try:
            collection.result.set(FactCollector()._collect(request))
        except BaseException as error:
            # Cancellation must also release waiters with a failure.
            collection.result.set_exception(error, exc_info())

    return deepcopy(collection.result.get())
