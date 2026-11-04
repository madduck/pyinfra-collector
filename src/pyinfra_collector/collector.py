"""Fact collection across the inventory.

Operations do not execute when they are called; they accumulate and run in a
later phase. Collection works the same way: calling :meth:`FactCollector.collect`
registers a fact, and :meth:`FactCollector.run` gathers everything registered so
far. That means a deploy can register facts as it goes, without knowing the full
list up front.

The :func:`collect` deploy flushes the accumulated registrations once per run.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

from pyinfra.api.deploy import deploy
from pyinfra.api.host import Host
from pyinfra.api.state import State
from pyinfra.context import ctx_host, ctx_state

from .store import FactClass, FactStore
from .store import fact_id as fact_id

#: Decides whether a host contributes a fact. May inspect the host object,
#: including its groups and data, and may itself call ``host.get_fact()``.
HostPredicate = Callable[[Host, FactClass], bool]


def _always(_host: Host, _fact: FactClass) -> bool:
    """Include every host."""
    return True


@dataclass
class FactRequest:
    """A registered request to collect one fact.

    Attributes:
        fact: The fact class to collect.
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
        """Return the bound arguments as JSON-compatible data, for storing."""
        return {"args": list(self.args), "kwargs": self.kwargs}

    @property
    def key(self) -> tuple[str, str]:
        """Return the key identifying this request within a collector."""
        return (self.id, json.dumps(self.args_data, sort_keys=True, default=str))


class FactCollector:
    """Accumulates fact requests and gathers them on demand.

    Registering a fact is cheap and side-effect free. Nothing is collected until
    :meth:`run` is called.
    """

    def __init__(self, store: FactStore) -> None:
        """Create a collector that writes to `store`.

        Args:
            store: The store to persist collected facts in.
        """
        self.store = store
        self._pending: dict[tuple[str, str], FactRequest] = {}

    def collect(
        self,
        fact: FactClass,
        *args: Any,
        include: HostPredicate = _always,
        **kwargs: Any,
    ) -> None:
        """Register a fact for collection. Does not collect now.

        Registering the same fact with the same arguments twice is a no-op.

        Args:
            fact: The fact class to collect.
            *args: Positional arguments for the fact's command.
            include: Predicate deciding which hosts contribute.
            **kwargs: Keyword arguments for the fact's command.
        """
        request = FactRequest(fact=fact, args=args, kwargs=kwargs, include=include)
        self._pending.setdefault(request.key, request)

    @property
    def pending(self) -> list[FactRequest]:
        """Return the registered requests, in registration order."""
        return list(self._pending.values())

    def run(self) -> int:
        """Collect every registered fact and persist it.

        The included hosts are gathered in parallel through ``state.fact_pool``.
        The predicate is applied once, before any host is contacted, so an
        excluded host is never touched. Writing to the store stays serial: it
        uses one SQLite connection, which is not shared across greenlets.

        Returns:
            The number of observations that changed and were stored.
        """
        state = cast(State, ctx_state.get())  # type: ignore[no-untyped-call]
        hosts = state.inventory.get_active_hosts()

        written = 0
        for request in self.pending:
            for host, data in self._gather(state, request, hosts):
                if self.store.record(host.name, request.id, data, request.args_data):
                    written += 1
        return written

    def _gather(
        self,
        state: State,
        request: FactRequest,
        hosts: list[Host],
    ) -> list[tuple[Host, Any]]:
        """Collect one fact from the hosts that pass its predicate, in parallel.

        Args:
            state: The current state, providing the fact pool.
            request: The fact to collect.
            hosts: Candidate hosts, in inventory order.

        Returns:
            Pairs of host and collected value, in inventory order.
        """
        selected = [host for host in hosts if request.include(host, request.fact)]

        def gather_one(host: Host) -> Any:
            # Facts may reach for the host-context global (e.g. server.Uptime
            # reads server.Kernel), so bind the context while collecting.
            with ctx_host.use(host):
                return host.get_fact(request.fact, *request.args, **request.kwargs)

        with ctx_state.use(state):
            results = {
                host: state.fact_pool.spawn(gather_one, host) for host in selected
            }

        return [(host, greenlet.get()) for host, greenlet in results.items()]

    def reset(self) -> None:
        """Forget all registered requests."""
        self._pending.clear()


#: Requests registered by :func:`register`, flushed by the :func:`collect`
#: deploy. Registration is open as soon as this module is imported; the store is
#: created on the first flush, so importing never touches the filesystem.
collector: FactCollector | None = None


def register(
    fact: FactClass,
    *args: Any,
    include: HostPredicate = _always,
    **kwargs: Any,
) -> None:
    """Register a fact for collection by the :func:`collect` deploy.

    This is the module-level counterpart of :meth:`FactCollector.collect`, for
    deploys that do not manage their own collector.

    Args:
        fact: The fact class to collect.
        *args: Positional arguments for the fact's command.
        include: Predicate deciding which hosts contribute.
        **kwargs: Keyword arguments for the fact's command.
    """
    global collector
    if collector is None:
        collector = FactCollector(FactStore())
    collector.collect(fact, *args, include=include, **kwargs)


@deploy()
def collect() -> None:
    """Gather all registered facts once per run and persist them.

    A deploy runs once per host, but collection is a whole-inventory operation.
    Only the first active host triggers it, so facts are gathered exactly once.
    The collector is flushed, so a second call in the same run does nothing.
    """
    if collector is None:
        return

    state = cast(State, ctx_state.get())  # type: ignore[no-untyped-call]
    host = cast(Host, ctx_host.get())  # type: ignore[no-untyped-call]
    if host is not state.inventory.get_active_hosts()[0]:
        return

    collector.run()
    collector.reset()
