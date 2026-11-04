import re
import time
import weakref
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from pyinfra.api.config import Config
from pyinfra.api.connect import connect_all
from pyinfra.api.deploy import add_deploy
from pyinfra.api.host import Host
from pyinfra.api.inventory import Inventory
from pyinfra.api.operations import run_ops
from pyinfra.api.state import State
from pyinfra.connectors.fake import FakeConnector
from pyinfra.context import ctx_state
from pyinfra.facts import server

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


def test_collect_registers_without_collecting(state: State, store: FactStore) -> None:
    collector = FactCollector(store)
    with ctx_state.use(state):
        collector.collect(server.Uptime)
        # Nothing gathered yet.
        assert store.latest_for("server.Uptime") == {}
    assert [request.id for request in collector.pending] == ["server.Uptime"]


def test_run_gathers_from_every_host(state: State, store: FactStore) -> None:
    collector = FactCollector(store)
    collector.collect(server.KernelVersion)
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
        collector.collect(
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
        collector.collect(server.KernelVersion)
        started = time.monotonic()
        with ctx_state.use(state):
            written = collector.run()
        elapsed = time.monotonic() - started

    assert written == count
    # Serial collection would take count * delay; parallel stays well under it.
    assert elapsed < count * delay / 2


def test_duplicate_registration_is_deduplicated(state: State, store: FactStore) -> None:
    collector = FactCollector(store)
    collector.collect(server.KernelVersion)
    collector.collect(server.KernelVersion)
    assert len(collector.pending) == 1


def test_registration_distinguishes_arguments(state: State, store: FactStore) -> None:
    collector = FactCollector(store)
    collector.collect(server.KernelVersion)
    collector.collect(server.KernelVersion, "extra")
    assert len(collector.pending) == 2


def test_include_predicate_filters_hosts(state: State, store: FactStore) -> None:
    collector = FactCollector(store)
    collector.collect(
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
    collector.collect(server.KernelVersion, include=linux_only)
    with ctx_state.use(state):
        collector.run()
    # The fake connector returns empty output for uname -s, so no host matches.
    assert store.latest_for("server.KernelVersion") == {}


def test_run_skips_unchanged_values(state: State, store: FactStore) -> None:
    collector = FactCollector(store)
    collector.collect(server.KernelVersion)
    with ctx_state.use(state):
        assert collector.run() == 2
        assert collector.run() == 0


def test_reset_forgets_registrations(state: State, store: FactStore) -> None:
    collector = FactCollector(store)
    collector.collect(server.KernelVersion)
    collector.reset()
    assert collector.pending == []


def test_collect_deploy_flushes_once(state: State, store: FactStore) -> None:
    collector = FactCollector(store)
    collector.collect(server.KernelVersion)
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


def test_collect_deploy_without_registrations_is_noop(
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


def test_register_creates_collector_lazily(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # the lazy default store lives in the cwd
    original = collect_module.collector
    try:
        collect_module.collector = None
        collect_module.register(server.KernelVersion)
        created: FactCollector = collect_module.collector  # type: ignore[assignment]
        assert [request.id for request in created.pending] == ["server.KernelVersion"]
    finally:
        if collect_module.collector is not None:
            collect_module.collector.store.close()
        collect_module.collector = original


def test_register_reuses_existing_collector(store: FactStore) -> None:
    original = collect_module.collector
    collect_module.collector = FactCollector(store)
    try:
        collect_module.register(server.KernelVersion)
        created = collect_module.collector
        assert created is not None
        assert created.store is store
        assert len(created.pending) == 1
    finally:
        collect_module.collector = original


def test_register_arms_prepare_end_once(
    state: State, store: FactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deploy file runs once per host; arming must still happen once per run."""
    monkeypatch.setattr(collect_module, "collector", FactCollector(store))
    monkeypatch.setattr(collect_module, "_armed", weakref.WeakSet())

    with ctx_state.use(state):
        collect_module.register(server.KernelVersion)
        collect_module.register(server.KernelVersion)

    handlers = [
        handler
        for handler in state.callback_handlers
        if isinstance(handler, collect_module.PrepareEndFlush)
    ]
    assert len(handlers) == 1


def test_prepare_end_collects_and_flushes(
    state: State, store: FactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Firing prepare_end gathers the facts registered during prepare."""
    monkeypatch.setattr(collect_module, "collector", FactCollector(store))
    monkeypatch.setattr(collect_module, "_armed", weakref.WeakSet())

    with ctx_state.use(state):
        collect_module.register(server.KernelVersion)
        state.trigger_callbacks("prepare_end")

    assert store.latest_for("server.KernelVersion") == {
        "@fake/node1": "6.1.0",
        "@fake/node2": "6.2.0",
    }
    # The flush forgets the registrations, so a second run does not re-collect.
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
        collect_module.register(server.KernelVersion)
        state.trigger_callbacks("prepare_end", **flags)

    assert store.latest_for("server.KernelVersion") == {
        "@fake/node1": "6.1.0",
        "@fake/node2": "6.2.0",
    }


def test_register_without_state_does_not_arm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registration outside a run has no state to arm, and must still work."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(collect_module, "collector", None)
    monkeypatch.setattr(collect_module, "_armed", weakref.WeakSet())

    collect_module.register(server.KernelVersion)

    assert collect_module.collector is not None
    assert len(collect_module._armed) == 0
    collect_module.collector.store.close()


def test_flush_without_registrations_is_noop() -> None:
    assert collect_module._flush() == 0
