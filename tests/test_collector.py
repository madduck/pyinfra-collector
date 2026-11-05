import json
import re
import time
import weakref
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import gevent
import pytest
from pyinfra.api.config import Config
from pyinfra.api.connect import connect_all
from pyinfra.api.deploy import add_deploy
from pyinfra.api.exceptions import PyinfraError
from pyinfra.api.host import Host
from pyinfra.api.inventory import Inventory
from pyinfra.api.operations import run_ops
from pyinfra.api.state import State
from pyinfra.connectors.fake import FakeConnector
from pyinfra.context import ctx_state
from pyinfra.facts import server
from pyinfra_cli.util import load_deploy_file

from pyinfra_collector import collector as collect_module
from pyinfra_collector.collector import FactCollector
from pyinfra_collector.store import FactStore
from pyinfra_collector.typedefs import fact_id

FAKE_HOMES = {
    "@fake/node1": {"fake_responses": {re.compile(r"uname -r"): "6.1.0"}},
    "@fake/node2": {"fake_responses": {re.compile(r"uname -r"): "6.2.0"}},
}


@pytest.fixture
def inventory() -> Inventory:
    return Inventory(  # type: ignore[no-untyped-call]
        (
            [(name, dict(data, fake_delay=0)) for name, data in FAKE_HOMES.items()],
            {},
        )
    )


@pytest.fixture
def state(inventory: Inventory) -> Iterator[State]:
    state = State(inventory, Config())  # type: ignore[no-untyped-call]
    connect_all(state)
    yield state


@pytest.fixture
def store(tmp_path: Path) -> Iterator[FactStore]:
    with FactStore(tmp_path / "facts.sqlite3") as store:
        yield store


def test_fact_id_uses_canonical_name() -> None:
    assert fact_id(server.Uptime) == "server.Uptime"


def test_gather_adds_without_collecting(state: State, store: FactStore) -> None:
    collector = FactCollector(store)
    with ctx_state.use(state):
        collector.gather(server.Uptime)
        # Nothing gathered yet.
        assert store.latest_for("server.Uptime") == {}
    assert [request.id for request in collector.pending] == ["server.Uptime"]


def test_run_gathers_from_every_host(state: State, store: FactStore) -> None:
    collector = FactCollector(store)
    collector.gather(server.KernelVersion)
    with ctx_state.use(state):
        written = collector.run()
    assert written == 2
    assert store.latest_for("server.KernelVersion") == {
        "@fake/node1": "6.1.0",
        "@fake/node2": "6.2.0",
    }


def test_excluded_host_is_never_contacted(tmp_path: Path) -> None:
    """The predicate is applied before gathering, so excluded hosts do no work."""
    inventory = Inventory(  # type: ignore[no-untyped-call]
        (
            [
                ("@fake/keep", {"fake_delay": 0}),
                ("@fake/skip", {"fake_delay": 0}),
            ],
            {},
        )
    )
    state = State(inventory, Config())  # type: ignore[no-untyped-call]
    connect_all(state)

    with FactStore(tmp_path / "facts.sqlite3") as store:
        collector = FactCollector(store)
        collector.gather(
            server.KernelVersion, include=lambda host, fact: host.name == "@fake/keep"
        )
        with ctx_state.use(state):
            collector.run()

    executed = {
        host.name
        for host in state.inventory.get_active_hosts()
        if cast(FakeConnector, host.connector).executed_commands
    }
    assert executed == {"@fake/keep"}


def test_gathering_is_parallel(tmp_path: Path) -> None:
    """Hosts are gathered concurrently, not one after another."""
    count = 8
    delay = 0.3
    inventory = Inventory(  # type: ignore[no-untyped-call]
        (
            [
                (
                    f"@fake/n{i}",
                    {
                        "fake_responses": {re.compile(r"uname -r"): "6.1.0"},
                        "fake_delay": delay,
                        "fake_delay_jitter": 0,
                    },
                )
                for i in range(count)
            ],
            {},
        )
    )
    state = State(inventory, Config(PARALLEL=count))  # type: ignore[no-untyped-call]
    connect_all(state)

    with FactStore(tmp_path / "facts.sqlite3") as store:
        collector = FactCollector(store)
        collector.gather(server.KernelVersion)
        started = time.monotonic()
        with ctx_state.use(state):
            written = collector.run()
        elapsed = time.monotonic() - started

    assert written == count
    # Serial collection would take count * delay; parallel stays well under it.
    assert elapsed < count * delay / 2


def test_duplicate_gather_is_deduplicated(state: State, store: FactStore) -> None:
    collector = FactCollector(store)
    collector.gather(server.KernelVersion)
    collector.gather(server.KernelVersion)
    assert len(collector.pending) == 1


def test_gather_distinguishes_arguments(state: State, store: FactStore) -> None:
    collector = FactCollector(store)
    collector.gather(server.KernelVersion)
    collector.gather(server.KernelVersion, "extra")
    assert len(collector.pending) == 2


def test_include_predicate_filters_hosts(state: State, store: FactStore) -> None:
    collector = FactCollector(store)
    collector.gather(
        server.KernelVersion, include=lambda host, fact: host.name.endswith("1")
    )
    with ctx_state.use(state):
        written = collector.run()
    assert written == 1
    assert store.latest_for("server.KernelVersion") == {"@fake/node1": "6.1.0"}


def test_include_predicate_may_itself_read_facts(
    state: State, store: FactStore
) -> None:
    """The predicate may call get_fact, e.g. to filter on kernel name."""

    def linux_only(host: Host, fact: type) -> bool:
        kernel: str = host.get_fact(server.Kernel)
        return kernel == "Linux"

    collector = FactCollector(store)
    collector.gather(server.KernelVersion, include=linux_only)
    with ctx_state.use(state):
        collector.run()
    # The fake connector returns empty output for uname -s, so no host matches.
    assert store.latest_for("server.KernelVersion") == {}


def test_run_skips_unchanged_values(state: State, store: FactStore) -> None:
    collector = FactCollector(store)
    collector.gather(server.KernelVersion)
    with ctx_state.use(state):
        assert collector.run() == 2
        assert collector.run() == 0


def test_reset_forgets_requests(state: State, store: FactStore) -> None:
    collector = FactCollector(store)
    collector.gather(server.KernelVersion)
    collector.reset()
    assert collector.pending == []


def test_collect_deploy_flushes_once(state: State, store: FactStore) -> None:
    collector = FactCollector(store)
    collector.gather(server.KernelVersion)
    original = collect_module.collector
    collect_module.collector = collector
    try:
        add_deploy(state, collect_module.collect)
        run_ops(state)
    finally:
        collect_module.collector = original

    assert store.latest_for("server.KernelVersion") == {
        "@fake/node1": "6.1.0",
        "@fake/node2": "6.2.0",
    }


def test_collect_deploy_without_requests_is_noop(
    state: State, store: FactStore
) -> None:
    original = collect_module.collector
    collect_module.collector = None
    try:
        add_deploy(state, collect_module.collect)
        run_ops(state)
    finally:
        collect_module.collector = original
    assert store.latest_for("server.KernelVersion") == {}


def test_gather_creates_collector_lazily(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # the lazy default store lives in the cwd
    original = collect_module.collector
    try:
        collect_module.collector = None
        collect_module.gather(server.KernelVersion)
        created: FactCollector = collect_module.collector  # type: ignore[assignment]
        assert [request.id for request in created.pending] == ["server.KernelVersion"]
    finally:
        if collect_module.collector is not None:
            collect_module.collector.store.close()
        collect_module.collector = original


def test_gather_reuses_existing_collector(store: FactStore) -> None:
    original = collect_module.collector
    collect_module.collector = FactCollector(store)
    try:
        collect_module.gather(server.KernelVersion)
        created = collect_module.collector
        assert created is not None
        assert created.store is store
        assert len(created.pending) == 1
    finally:
        collect_module.collector = original


def test_gather_arms_prepare_end_once(
    state: State, store: FactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deploy file runs once per host; arming must still happen once per run."""
    monkeypatch.setattr(collect_module, "collector", FactCollector(store))
    monkeypatch.setattr(collect_module, "_armed", weakref.WeakSet())

    with ctx_state.use(state):
        collect_module.gather(server.KernelVersion)
        collect_module.gather(server.KernelVersion)

    handlers = [
        handler
        for handler in state.callback_handlers
        if isinstance(handler, collect_module.PrepareEndFlush)
    ]
    assert len(handlers) == 1


def test_prepare_end_collects_and_flushes(
    state: State, store: FactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Firing prepare_end gathers the facts added during prepare."""
    monkeypatch.setattr(collect_module, "collector", FactCollector(store))
    monkeypatch.setattr(collect_module, "_armed", weakref.WeakSet())

    with ctx_state.use(state):
        collect_module.gather(server.KernelVersion)
        state.trigger_callbacks("prepare_end")

    assert store.latest_for("server.KernelVersion") == {
        "@fake/node1": "6.1.0",
        "@fake/node2": "6.2.0",
    }
    # The flush forgets the requests, so a second run does not gather again.
    assert collect_module.collector is not None
    assert collect_module.collector.pending == []


@pytest.mark.parametrize("flags", [{"dry": True}, {"debug": True}])
def test_prepare_end_collects_on_dry_and_debug_runs(
    state: State,
    store: FactStore,
    monkeypatch: pytest.MonkeyPatch,
    flags: dict[str, bool],
) -> None:
    """Collection is read-only, so it happens even when no operation will run."""
    monkeypatch.setattr(collect_module, "collector", FactCollector(store))
    monkeypatch.setattr(collect_module, "_armed", weakref.WeakSet())

    with ctx_state.use(state):
        collect_module.gather(server.KernelVersion)
        state.trigger_callbacks("prepare_end", **flags)

    assert store.latest_for("server.KernelVersion") == {
        "@fake/node1": "6.1.0",
        "@fake/node2": "6.2.0",
    }


def test_gather_without_state_does_not_arm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registration outside a run has no state to arm, and must still work."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(collect_module, "collector", None)
    monkeypatch.setattr(collect_module, "_armed", weakref.WeakSet())

    collect_module.gather(server.KernelVersion)

    assert collect_module.collector is not None
    assert len(collect_module._armed) == 0
    collect_module.collector.store.close()


def test_flush_without_requests_is_noop() -> None:
    assert collect_module._flush() == 0


BARRIER_DEPLOY = """
import json
from pathlib import Path

from pyinfra.context import ctx_host
from pyinfra.facts import server
from pyinfra_collector.collector import collect, gather
from pyinfra_collector.store import FactStore

gather(server.KernelVersion)
collect()

with FactStore() as store:
    data = store.latest_for(server.KernelVersion)

name = ctx_host.get().name
Path({out!r}).joinpath(name.replace("/", "_")).write_text(json.dumps(data))
"""


@pytest.fixture
def parallel_state() -> Iterator[State]:
    """Four fake hosts, enough for the parallel load to race."""
    inventory = Inventory(  # type: ignore[no-untyped-call]
        (
            [
                (
                    f"@fake/node{i}",
                    {
                        "fake_responses": {re.compile(r"uname -r"): "6.1.0"},
                        "fake_delay": 0,
                    },
                )
                for i in range(4)
            ],
            {},
        )
    )
    state = State(inventory, Config())  # type: ignore[no-untyped-call]
    connect_all(state)
    yield state


def test_collect_deploy_barriers_parallel_hosts(
    parallel_state: State, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every host reads the store only after the single flush commits.

    The deploy file runs once per host in parallel. Without the barrier one host
    flushes and the rest read an empty store before the flush commits.
    """
    monkeypatch.chdir(tmp_path)  # the lazy default store lives in the cwd
    monkeypatch.setattr(collect_module, "collector", None)
    monkeypatch.setattr(collect_module, "_armed", weakref.WeakSet())
    monkeypatch.setattr(collect_module, "_barriers", weakref.WeakKeyDictionary())

    out = tmp_path / "out"
    out.mkdir()
    deploy_file = tmp_path / "deploy.py"
    deploy_file.write_text(BARRIER_DEPLOY.format(out=str(out)))

    flushes: list[int] = []
    original_flush = collect_module._flush

    def counting_flush() -> int:
        flushes.append(1)
        return original_flush()

    monkeypatch.setattr(collect_module, "_flush", counting_flush)

    with ctx_state.use(parallel_state):
        load_deploy_file(parallel_state, str(deploy_file))

    results = {path.name: json.loads(path.read_text()) for path in out.iterdir()}
    assert set(results) == {
        "@fake_node0",
        "@fake_node1",
        "@fake_node2",
        "@fake_node3",
    }
    assert all(len(values) == 4 for values in results.values())
    assert len(flushes) == 1


@pytest.mark.parametrize("parallel", [1, 2, 3])
def test_collect_deploy_rejects_small_parallel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parallel: int
) -> None:
    """A pool too small to load every host at once fails clearly, not by hanging.

    With fewer pool slots than hosts, pyinfra loads the hosts in waves. The
    hosts that run would block in the barrier waiting for hosts the pool cannot
    schedule, and gevent would abort with an opaque ``LoopExit``. The run must
    instead raise a clear error before anything blocks.
    """
    inventory = Inventory(  # type: ignore[no-untyped-call]
        (
            [
                (
                    f"@fake/node{i}",
                    {
                        "fake_responses": {re.compile(r"uname -r"): "6.1.0"},
                        "fake_delay": 0,
                    },
                )
                for i in range(4)
            ],
            {},
        )
    )
    state = State(inventory, Config(PARALLEL=parallel))  # type: ignore[no-untyped-call]
    connect_all(state)

    monkeypatch.chdir(tmp_path)  # the lazy default store lives in the cwd
    monkeypatch.setattr(collect_module, "collector", None)
    monkeypatch.setattr(collect_module, "_armed", weakref.WeakSet())
    monkeypatch.setattr(collect_module, "_barriers", weakref.WeakKeyDictionary())

    out = tmp_path / "out"
    out.mkdir()
    deploy_file = tmp_path / "deploy.py"
    deploy_file.write_text(BARRIER_DEPLOY.format(out=str(out)))

    with ctx_state.use(state), pytest.raises(PyinfraError, match="PARALLEL"):
        load_deploy_file(state, str(deploy_file))


def test_barrier_blocks_until_last_arrival(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first arrivals wait; only the last one flushes."""
    flushes: list[int] = []

    def counting_flush() -> int:
        flushes.append(1)
        return 0

    monkeypatch.setattr(collect_module, "_flush", counting_flush)

    order: list[str] = []
    barrier = collect_module._CollectBarrier(expected=2)

    def waiter() -> None:
        barrier.wait()
        order.append("waiter")

    def flusher() -> None:
        barrier.wait()
        order.append("flusher")

    waiter_greenlet = gevent.spawn(waiter)
    gevent.sleep(0)  # let the waiter arrive and block
    assert order == []
    assert flushes == []

    flusher_greenlet = gevent.spawn(flusher)
    waiter_greenlet.join()
    flusher_greenlet.join()

    assert flushes == [1]
    assert set(order) == {"waiter", "flusher"}


def test_barrier_ignores_calls_after_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once a flush happened, a later call does nothing."""
    flushes: list[int] = []

    def counting_flush() -> int:
        flushes.append(1)
        return 0

    monkeypatch.setattr(collect_module, "_flush", counting_flush)

    barrier = collect_module._CollectBarrier(expected=1)
    barrier.wait()
    barrier.wait()
    assert flushes == [1]
