"""Fact collection across the inventory.

Operations do not execute when they are called; they accumulate and run in a
later phase. Collection works the same way: calling :meth:`FactCollector.gather`
adds a fact, and :meth:`FactCollector.run` gathers everything added so far. That
means a deploy can gather facts as it goes, without knowing the full list up
front.

The first :func:`gather` call arms a :class:`PrepareEndFlush` on the current run,
and pyinfra fires it at the end of the prepare phase, so collection happens
automatically. See :func:`collect` for the fallback when pyinfra does not fire the
``prepare_end`` callback.
"""

from __future__ import annotations

import json
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

from gevent import getcurrent
from gevent.event import Event
from pyinfra.api.deploy import deploy
from pyinfra.api.exceptions import PyinfraError
from pyinfra.api.host import Host
from pyinfra.api.state import BaseStateCallback, State
from pyinfra.context import ctx_host, ctx_state

from .store import FactStore
from .typedefs import FactClass, args_data, fact_id

#: Decides whether a host contributes a fact. May inspect the host object,
#: including its groups and data, and may itself call ``host.get_fact()``.
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
        """Return the bound arguments as JSON-compatible data, for storing."""
        return args_data(self.args, self.kwargs)

    @property
    def key(self) -> tuple[str, str]:
        """Return the key identifying this request within a collector."""
        return (self.id, json.dumps(self.args_data, sort_keys=True, default=str))


class FactCollector:
    """Accumulates fact requests and gathers them on demand.

    Adding a fact is cheap and side-effect free. Nothing is collected until
    :meth:`run` is called.
    """

    def __init__(self, store: FactStore) -> None:
        """Create a collector that writes to `store`.

        Args:
            store: The store to persist collected facts in.
        """
        self.store = store
        self._pending: dict[tuple[str, str], FactRequest] = {}

    def gather(
        self,
        fact: FactClass,
        *args: Any,
        include: HostPredicate = _always,
        **kwargs: Any,
    ) -> None:
        """Add a fact to gather. Does not gather it now.

        Adding the same fact with the same arguments twice is a no-op.

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
        """Return the pending requests, in the order they were added."""
        return list(self._pending.values())

    def run(self) -> int:
        """Gather every pending fact and persist it.

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
                if self.store._record(host.name, request.id, data, request.args_data):
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
        """Forget all pending requests."""
        self._pending.clear()


#: Requests added by :func:`gather`, flushed at the end of the prepare phase by
#: :class:`PrepareEndFlush`. Gathering is open as soon as this module is imported;
#: the store is created on the first :func:`gather` call, so importing never
#: touches the filesystem.
collector: FactCollector | None = None

#: States that already carry a :class:`PrepareEndFlush`. A deploy file is loaded
#: once per host, so ``gather`` runs once per host and must arm the hook only once
#: per run.
_armed: weakref.WeakSet[State] = weakref.WeakSet()


class _CollectBarrier:
    """Coordinates the single flush among the parallel per-host deploy runs.

    A deploy file runs once per active host, in parallel greenlets, and every
    run reaches :func:`collect`. Collection is a whole-inventory operation, and
    it must cover every host's :func:`gather` calls, so no host may read the
    store before the flush commits. Each arrival is recorded; the final one
    performs the flush and releases the rest.
    """

    def __init__(self, expected: int) -> None:
        """Create a barrier that waits for `expected` arrivals.

        Args:
            expected: Number of active hosts that will reach :func:`collect`.
        """
        self.expected = expected
        self.arrivals = 0
        self._done = Event()

    def wait(self) -> None:
        """Record one arrival, flushing once every host has arrived.

        Every arrival but the last blocks until the flush commits. The last
        arrival runs the flush, then releases the waiters. If a flush already
        happened this run, a later call does nothing.
        """
        if self._done.is_set():
            return
        self.arrivals += 1
        if self.arrivals < self.expected:
            self._done.wait()
            return
        try:
            _flush()
        finally:
            self._done.set()


#: The collect barrier for each run, created on first :func:`collect` and held
#: until the run's state is garbage collected. A deploy file runs once per host,
#: so the arrivals must be counted per run, not in a module global.
_barriers: weakref.WeakKeyDictionary[State, _CollectBarrier] = (
    weakref.WeakKeyDictionary()
)


def _flush() -> int:
    """Gather every pending fact and forget the requests.

    Returns:
        The number of observations that changed and were stored. Zero when
        nothing is pending.
    """
    if collector is None:
        return 0
    written = collector.run()
    collector.reset()
    return written


def _barrier_for(state: State) -> _CollectBarrier | None:
    """Return the run's collect barrier, or `None` outside a parallel load.

    pyinfra loads a deploy file in one greenlet per host, all spawned on
    ``state.pool``. Only there do the per-host runs race, so only there is a
    barrier needed and safe. ``add_deploy`` (the Python API) runs the deploy
    serially on the caller's greenlet, where waiting for other arrivals would
    deadlock; that path keeps the first-host flush below.

    The barrier needs every active host's load greenlet to be running at once,
    so the pool must be large enough to hold them all. When it is not, the
    hosts that do run would block in the barrier waiting for hosts the pool
    cannot schedule, and gevent would abort the run with an opaque ``LoopExit``.
    That precondition is unsatisfiable, so raise instead, before blocking.

    Args:
        state: The current run's state.

    Returns:
        The barrier for this run, created on first use, or `None` when not
        running inside a pool-spawned load greenlet.

    Raises:
        PyinfraError: If the pool cannot load every active host concurrently.
    """
    if getcurrent() not in state.pool.greenlets:
        return None

    expected = len(state.inventory.get_active_hosts())
    if state.pool.size < expected:
        raise PyinfraError(
            "collect() needs every active host to load at the same time, but "
            f"PARALLEL is {state.pool.size} and there are {expected} active "
            "hosts. Pyinfra would load the hosts in waves, so the hosts that "
            "start would wait in the collection barrier for hosts that cannot "
            "be scheduled. Raise PARALLEL to at least the number of active "
            "hosts, or gather and collect in one invocation and read the store "
            "in another.",
        )

    try:
        return _barriers[state]
    except KeyError:
        barrier = _CollectBarrier(expected)
        _barriers[state] = barrier
        return barrier


class PrepareEndFlush(BaseStateCallback):
    """Gather pending facts at the end of pyinfra's prepare phase.

    pyinfra fires ``prepare_end`` once per run, after the deploy code has run and
    change detection is done, but before any operation executes. The handler
    gathers everything :func:`gather` accumulated during prepare and writes it to
    the store.

    Collection is read-only, so it runs for dry runs and debug dumps as well;
    the store is populated even when the run does not go on to execute.
    """

    def prepare_end(self, state: State, dry: bool = False, debug: bool = False) -> None:
        """Flush the module collector.

        Args:
            state: The current run's state. Unused; collection reads the host
                and state context globals itself.
            dry: Whether the run is a dry run. Recorded but not acted on, since
                collecting facts changes nothing on the hosts.
            debug: Whether the run dumps facts or operations and exits. As with
                ``dry``, collection still happens.
        """
        _flush()


def _arm(state: State) -> None:
    """Attach a :class:`PrepareEndFlush` to `state`, at most once per state.

    Args:
        state: The state to arm.
    """
    if state in _armed:
        return
    state.add_callback_handler(PrepareEndFlush())  # type: ignore[no-untyped-call]
    _armed.add(state)


def gather(
    fact: FactClass,
    *args: Any,
    include: HostPredicate = _always,
    **kwargs: Any,
) -> None:
    """Add a fact to gather at the end of the prepare phase.

    This is the module-level counterpart of :meth:`FactCollector.gather`, for
    deploys that do not manage their own collector. The first call also arms a
    :class:`PrepareEndFlush` on the current state, so collection happens
    automatically and the deploy does not need to call :func:`collect`.

    Args:
        fact: The fact class to gather.
        *args: Positional arguments for the fact's command.
        include: Predicate deciding which hosts contribute.
        **kwargs: Keyword arguments for the fact's command.
    """
    global collector
    if collector is None:
        collector = FactCollector(FactStore())
    collector.gather(fact, *args, include=include, **kwargs)

    state = cast(State | None, ctx_state.get())  # type: ignore[no-untyped-call]
    if state is not None:
        _arm(state)


@deploy()
def collect() -> None:
    """Gather all pending facts once per run and persist them.

    This is only needed when :func:`gather` could not arm the automatic handler,
    which happens when pyinfra does not fire the ``prepare_end`` callback. When
    it does, collection runs on its own and calling this deploy is harmless but
    redundant.

    A deploy file runs once per active host, in parallel, and every active host
    reaches this call. Because collection is a whole-inventory operation, the
    calls act as a barrier: every host waits until the last one has arrived, then
    exactly one flushes the collector and the rest resume. Waiting for all
    arrivals matters: a host that flushed on its own would miss the facts a later
    host's :func:`gather` call has still to add. No host reads the store before
    the flush commits.

    Every active host must reach this call. A deploy that calls it for only some
    of the active hosts leaves the barrier waiting forever. A module-level call
    is reached by every active host, which is the intended use.
    """
    if collector is None:
        return

    state = cast(State, ctx_state.get())  # type: ignore[no-untyped-call]
    barrier = _barrier_for(state)
    if barrier is not None:
        barrier.wait()
        return

    # ``add_deploy`` runs the deploy serially on the caller's greenlet, with no
    # parallel runs to synchronise. Flush on the first active host instead.
    host = cast(Host, ctx_host.get())  # type: ignore[no-untyped-call]
    if host is not state.inventory.get_active_hosts()[0]:
        return
    _flush()
