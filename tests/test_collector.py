import json
import re
import sqlite3
from collections.abc import Callable
from io import StringIO
from pathlib import Path
from traceback import extract_tb
from typing import Any, cast

import gevent
import pytest
from paramiko import SSHException
from pyinfra.api.command import FileUploadCommand
from pyinfra.api.config import Config
from pyinfra.api.connect import connect_all
from pyinfra.api.deploy import add_deploy
from pyinfra.api.exceptions import ConnectError, PyinfraError
from pyinfra.api.host import Host
from pyinfra.api.inventory import Inventory
from pyinfra.api.state import State
from pyinfra.connectors.fake import FakeConnector
from pyinfra.context import ctx_host, ctx_state
from pyinfra.facts import files, server
from pyinfra_cli.util import load_deploy_file

from pyinfra_collector import collect
from pyinfra_collector.collector import FactCollector
from pyinfra_collector.store import FactStore


@pytest.fixture
def make_state() -> Callable[..., State]:
    def create(parallel: int = 4, limit: str | None = None) -> State:
        inventory = Inventory(  # type: ignore[no-untyped-call]
            (
                [
                    (
                        f"@fake/node{i}",
                        {
                            "fake_responses": {re.compile(r"uname -r"): f"6.{i}.0"},
                            "fake_delay": 0,
                            "fake_delay_jitter": 0,
                        },
                    )
                    for i in range(4)
                ],
                {},
            )
        )
        state = State(inventory, Config(PARALLEL=parallel))  # type: ignore[no-untyped-call]
        if limit is not None:
            state.limit_hosts = [inventory.hosts[limit]]
        connect_all(state)
        return state

    return create


@pytest.fixture
def state(make_state: Callable[..., State]) -> State:
    return make_state()


@pytest.fixture(autouse=True)
def isolated_collections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


@pytest.mark.parametrize("outcome", ["success", "missing", "excluded", "error"])
def test_collection_logging_reports_only_successful_hosts(
    state: State,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outcome: str,
) -> None:
    state.print_fact_info = True

    def get_fact(host: Host, fact: type, *args: Any, **kwargs: Any) -> Any:
        if outcome == "error":
            raise ValueError("invalid fact")
        if outcome == "missing" and host.name == "@fake/node1":
            raise OSError("offline")
        return "private value"

    monkeypatch.setattr(Host, "get_fact", get_fact)
    collector = FactCollector()
    collector.gather(
        server.KernelVersion,
        include=lambda host, fact: (
            outcome != "excluded" and host.name in {"@fake/node0", "@fake/node1"}
        ),
    )
    with ctx_state.use(state):
        if outcome == "error":
            with pytest.raises(ValueError, match="invalid fact"):
                collector.run()
        else:
            collector.run()
    messages = [record.getMessage() for record in caplog.records]
    assert messages.count("--> Collecting server.KernelVersion from hosts...") == 1
    if outcome == "error":
        assert not any("--> Collected" in message for message in messages)
    else:
        counts = {"success": "2/2", "missing": "1/2", "excluded": "0/0"}
        assert (
            f"--> Collected server.KernelVersion from {counts[outcome]} selected hosts."
            in messages
        )
    available = [message for message in messages if "Available fact:" in message]
    expected = {"success": 2, "missing": 1, "excluded": 0, "error": 0}
    assert len(available) == expected[outcome]
    assert all("@fake/node2" not in message for message in available)
    assert all("private value" not in message for message in messages)


def test_gather_adds_without_collecting(tmp_path: Path) -> None:
    collector = FactCollector()
    collector.gather(server.KernelVersion)
    assert list(tmp_path.iterdir()) == []
    assert [request.id for request in collector.pending] == ["server.KernelVersion"]


def test_run_gathers_from_every_host(state: State) -> None:
    collector = FactCollector()
    collector.gather(server.KernelVersion)
    with ctx_state.use(state):
        results = collector.run()
    assert len(results) == 1
    request, values = results[0]
    assert request.fact is server.KernelVersion
    assert values == {f"@fake/node{i}": f"6.{i}.0" for i in range(4)}


def test_excluded_host_is_never_contacted(state: State) -> None:
    collector = FactCollector()
    collector.gather(
        server.KernelVersion, include=lambda host, fact: host.name == "@fake/node0"
    )
    with ctx_state.use(state):
        assert collector.run()[0][1] == {"@fake/node0": "6.0.0"}
    executed = {
        host.name
        for host in state.inventory.get_active_hosts()
        if cast(FakeConnector, host.connector).executed_commands
    }
    assert executed == {"@fake/node0"}


def test_duplicate_gather_is_deduplicated() -> None:
    collector = FactCollector()
    collector.gather(server.KernelVersion)
    collector.gather(server.KernelVersion)
    assert len(collector.pending) == 1


def test_gather_distinguishes_arguments() -> None:
    collector = FactCollector()
    collector.gather(files.File, "/a", mode=True)
    collector.gather(files.File, "/b", mode=True)
    assert len(collector.pending) == 2
    assert collector.pending[0].args_data == {
        "args": ["/a"],
        "kwargs": {"mode": True},
    }


def test_predicate_selects_whole_inventory_before_connections(
    make_state: Callable[..., State], monkeypatch: pytest.MonkeyPatch
) -> None:
    state = make_state(limit="@fake/node0")
    visits: list[str] = []
    connections: list[str] = []
    original = Host.connect

    def connect(host: Host, *args: Any, **kwargs: Any) -> Any:
        connections.append(host.name)
        return original(host, *args, **kwargs)

    def include(host: Host, fact: type) -> bool:
        assert connections == []
        assert fact is server.KernelVersion
        assert host.data.fake_delay == 0
        assert "@fake" in host.groups
        visits.append(host.name)
        return host.name in {"@fake/node1", "@fake/node2"}

    monkeypatch.setattr(Host, "connect", connect)
    with ctx_state.use(state):
        values = collect(server.KernelVersion, include=include)
    assert set(visits) == {f"@fake/node{i}" for i in range(4)}
    assert len(visits) == 4
    assert set(connections) == {"@fake/node1", "@fake/node2"}
    assert values == {"@fake/node1": "6.1.0", "@fake/node2": "6.2.0"}
    assert state.inventory.hosts["@fake/node0"].connected
    assert not state.inventory.hosts["@fake/node1"].connected
    assert not state.inventory.hosts["@fake/node2"].connected
    assert not state.inventory.hosts["@fake/node3"].connected


def test_queue_results_can_be_explicitly_persisted(
    state: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    def get_fact(host: Host, fact: type, *args: Any, **kwargs: Any) -> Any:
        return args[0] if args else kwargs["path"]

    monkeypatch.setattr(Host, "get_fact", get_fact)
    collector = FactCollector()
    collector.gather(files.File, "/a", flag=True)
    collector.gather(files.File, path="/b")
    with ctx_state.use(state):
        results = collector.run()
    assert [request.args for request, values in results] == [("/a",), ()]
    with FactStore() as store:
        for request, values in results:
            assert (
                store.record_for(request.fact, values, *request.args, **request.kwargs)
                == 4
            )
        assert set(store.latest_for(files.File, "/a", flag=True).values()) == {"/a"}
        assert set(store.latest_for(files.File, path="/b").values()) == {"/b"}


def test_run_keeps_requests_until_reset(state: State) -> None:
    collector = FactCollector()
    collector.gather(server.KernelVersion)
    with ctx_state.use(state):
        first = collector.run()
        assert collector.run() == first
    assert collector.pending == [first[0][0]]


def test_reset_forgets_requests() -> None:
    collector = FactCollector()
    collector.gather(server.KernelVersion)
    collector.reset()
    assert collector.pending == []
    assert collector.run() == []


def test_collect_does_not_open_or_write_sqlite(
    state: State, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def connect(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Collection must not open SQLite")

    monkeypatch.setattr(sqlite3, "connect", connect)
    with ctx_state.use(state):
        values = collect(server.KernelVersion)
    assert values == {f"@fake/node{i}": f"6.{i}.0" for i in range(4)}
    assert not (tmp_path / ".pyinfra-collector.sqlite3").exists()


def test_collect_works_in_python_api(state: State) -> None:
    results: list[dict[str, Any]] = []

    def deploy() -> None:
        results.append(collect(server.KernelVersion))

    add_deploy(state, deploy)
    assert len(results) == 4
    assert all(len(result) == 4 for result in results)


DEPLOY = """
import json
from io import StringIO
from pathlib import Path

from pyinfra.context import ctx_host
from pyinfra.facts import server
from pyinfra.operations import files
from pyinfra_collector import collect

values = collect(server.KernelVersion)
Path({out!r}).joinpath(ctx_host.get().name.replace("/", "_")).write_text(
    json.dumps(values)
)
files.template(
    src=StringIO('{{{{ values | tojson }}}}'),
    dest='/var/tmp/fleet-kernels',
    values=values,
)
"""


@pytest.mark.parametrize("parallel", [1, 2, 3, 4])
def test_collect_before_template_with_capped_parallel(
    make_state: Callable[..., State], tmp_path: Path, parallel: int
) -> None:
    state = make_state(parallel)
    out = tmp_path / "out"
    out.mkdir()
    deploy_file = tmp_path / "deploy.py"
    deploy_file.write_text(DEPLOY.format(out=str(out)))
    with ctx_state.use(state), gevent.Timeout(5):
        load_deploy_file(state, str(deploy_file))
    expected = {f"@fake/node{i}": f"6.{i}.0" for i in range(4)}
    assert len(list(out.iterdir())) == 4
    assert all(json.loads(path.read_text()) == expected for path in out.iterdir())
    rendered: list[dict[str, str]] = []
    for host, operations in state.ops.items():
        with ctx_state.use(state), ctx_host.use(host):
            for operation in operations.values():
                for command in operation.command_generator():
                    if isinstance(command, FileUploadCommand):
                        rendered.append(
                            json.loads(cast(StringIO, command.src).getvalue())
                        )
    assert rendered == [expected] * 4


@pytest.mark.parametrize("parallel", [1, 2, 4])
def test_collect_fans_out_using_existing_connections(
    make_state: Callable[..., State], monkeypatch: pytest.MonkeyPatch, parallel: int
) -> None:
    state = make_state(parallel)
    hosts = state.inventory.get_active_hosts()
    connectors = {host.name: host.connector for host in hosts}
    entered: list[str] = []
    active = 0
    peak = 0
    original = Host.get_fact

    def get_fact(host: Host, fact: type, *args: Any, **kwargs: Any) -> Any:
        nonlocal active, peak
        assert fact is server.KernelVersion
        assert host.connector is connectors[host.name]
        assert ctx_host.get() is host  # type: ignore[no-untyped-call]
        assert ctx_state.get() is host.state  # type: ignore[no-untyped-call]
        assert host.state.fact_pool is state.fact_pool
        entered.append(host.name)
        active += 1
        peak = max(peak, active)
        gevent.sleep(0.01)
        try:
            return original(host, fact, *args, **kwargs)
        finally:
            active -= 1

    def reconnect(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Collection must reuse the connected hosts")

    monkeypatch.setattr(Host, "get_fact", get_fact)
    monkeypatch.setattr(FakeConnector, "connect", reconnect)
    with ctx_state.use(state):
        assert len(collect(server.KernelVersion)) == 4
    assert set(entered) == {host.name for host in hosts}
    assert len(entered) == 4
    assert peak == parallel
    assert all(
        len(cast(FakeConnector, host.connector).executed_commands) == 1
        for host in hosts
    )


def test_concurrent_and_later_callers_share_one_collection(
    state: State,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[Host] = []
    original = Host.get_fact

    def get_fact(host: Host, fact: type, *args: Any, **kwargs: Any) -> Any:
        calls.append(host)
        gevent.sleep(0.01)
        return original(host, fact, *args, **kwargs)

    def caller() -> dict[str, Any]:
        with ctx_state.use(state):
            return collect(server.KernelVersion)

    monkeypatch.setattr(Host, "get_fact", get_fact)
    callers = [gevent.spawn(caller) for _ in range(6)]
    results = [caller.get(timeout=5) for caller in callers]
    assert len(calls) == 4
    assert all(result == results[0] for result in results)
    results[0].clear()
    assert len(caller()) == 4
    assert len(calls) == 4
    messages = [record.getMessage() for record in caplog.records]
    assert messages.count("--> Collecting server.KernelVersion from hosts...") == 1
    assert (
        messages.count("--> Collected server.KernelVersion from 4/4 selected hosts.")
        == 1
    )


def test_native_get_fact_does_not_reuse_collector_cache(state: State) -> None:
    def gather_deploy() -> None:
        collect(server.KernelVersion)

    def native_deploy() -> None:
        host = cast(Host, ctx_host.get())  # type: ignore[no-untyped-call]
        host.get_fact(server.KernelVersion)

    add_deploy(state, gather_deploy)
    add_deploy(state, gather_deploy)
    assert all(
        len(cast(FakeConnector, host.connector).executed_commands) == 1
        for host in state.inventory
    )
    add_deploy(state, native_deploy)
    assert all(
        len(cast(FakeConnector, host.connector).executed_commands) == 2
        for host in state.inventory
    )


@pytest.mark.parametrize(
    "failure", [RuntimeError("fact collection failed"), gevent.GreenletExit()]
)
def test_collect_failure_reaches_every_caller(
    state: State,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    tmp_path: Path,
) -> None:
    attempts = 0
    original = FactCollector._gather

    def fail(*args: Any, **kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        gevent.sleep(0.01)
        raise failure

    def caller() -> BaseException:
        with ctx_state.use(state):
            try:
                collect(server.KernelVersion)
            except BaseException as error:
                return error
        raise AssertionError("Collection did not fail")

    monkeypatch.setattr(FactCollector, "_gather", fail)
    callers = [gevent.spawn(caller) for _ in range(4)]
    assert [caller.get(timeout=5) for caller in callers] == [failure] * 4
    monkeypatch.setattr(FactCollector, "_gather", original)
    assert caller() is failure
    assert attempts == 1
    assert any(frame.name == "fail" for frame in extract_tb(failure.__traceback__))
    assert not (tmp_path / ".pyinfra-collector.sqlite3").exists()


def test_collect_distinguishes_arguments_and_forwards_them(
    state: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def get_fact(host: Host, fact: type, *args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        return args[0] if args else kwargs["path"]

    monkeypatch.setattr(Host, "get_fact", get_fact)
    with ctx_state.use(state):
        first = collect(files.File, "/a", flag=True)
        second = collect(files.File, path="/b")
        assert collect(files.File, "/a", flag=True) == first
    assert set(first.values()) == {"/a"}
    assert set(second.values()) == {"/b"}
    assert (
        calls
        == [(("/a",), {"flag": True, "_ignore_errors": False})] * 4
        + [((), {"path": "/b", "_ignore_errors": False})] * 4
    )


def test_collect_distinguishes_predicates(state: State) -> None:
    def first(host: Host, fact: type) -> bool:
        return host.name.endswith("0")

    def second(host: Host, fact: type) -> bool:
        return host.name.endswith("1")

    with ctx_state.use(state):
        assert collect(server.KernelVersion, include=first) == {"@fake/node0": "6.0.0"}
        assert collect(server.KernelVersion, include=second) == {"@fake/node1": "6.1.0"}
        assert collect(server.KernelVersion, include=lambda host, fact: False) == {}
        assert collect(server.KernelVersion, include=first) == {"@fake/node0": "6.0.0"}


def test_collection_ignores_host_limit(make_state: Callable[..., State]) -> None:
    state = make_state(limit="@fake/node0")
    targets = set(state.inventory.get_active_hosts())
    activated = state.activated_hosts.copy()
    with ctx_state.use(state):
        assert collect(server.KernelVersion) == {
            f"@fake/node{i}": f"6.{i}.0" for i in range(4)
        }
    assert set(state.inventory.get_active_hosts()) == targets
    assert state.activated_hosts == activated
    assert state.failed_hosts == set()
    assert {host.name for host in state.inventory if host.connected} == {"@fake/node0"}


def test_collection_is_isolated_between_states(state: State) -> None:
    with ctx_state.use(state):
        first = collect(server.KernelVersion)
    inventory = Inventory(  # type: ignore[no-untyped-call]
        ([("@fake/other", {"fake_delay": 0, "fake_delay_jitter": 0})], {})
    )
    other = State(inventory, Config())  # type: ignore[no-untyped-call]
    connect_all(other)
    with ctx_state.use(other):
        assert collect(server.KernelVersion) == {"@fake/other": None}
    with ctx_state.use(state):
        assert collect(server.KernelVersion) == first


def test_collect_from_only_one_deploy_host(
    make_state: Callable[..., State], tmp_path: Path
) -> None:
    state = make_state(parallel=1)
    deploy_file = tmp_path / "subset.py"
    deploy_file.write_text(
        "from pathlib import Path\n"
        "from pyinfra import host\n"
        "from pyinfra.facts import server\n"
        "from pyinfra_collector import collect\n"
        "if host.name == '@fake/node0':\n"
        "    values = collect(server.KernelVersion)\n"
        "    Path('subset-count').write_text(str(len(values)))\n"
    )
    with ctx_state.use(state), gevent.Timeout(5):
        load_deploy_file(state, str(deploy_file))
    assert (tmp_path / "subset-count").read_text() == "4"
    assert not (tmp_path / ".pyinfra-collector.sqlite3").exists()


def test_explicit_persistence_failure_does_not_poison_collection(
    state: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("database write failed")

    with ctx_state.use(state):
        values = collect(server.KernelVersion)
    original = FactStore._record
    with FactStore() as store:
        monkeypatch.setattr(FactStore, "_record", fail)
        with pytest.raises(RuntimeError, match="database write failed"):
            store.record_for(server.KernelVersion, values)
        with ctx_state.use(state):
            assert collect(server.KernelVersion) == values
        monkeypatch.setattr(FactStore, "_record", original)
        assert store.record_for(server.KernelVersion, values) == 4
        assert store.latest_for(server.KernelVersion) == values


def test_collect_leaves_existing_store_untouched(state: State) -> None:
    with FactStore() as store:
        store.record_for(server.KernelVersion, {"retired-node": "previous"})
    with ctx_state.use(state):
        assert len(collect(server.KernelVersion)) == 4
    with FactStore() as store:
        assert store.latest_for(server.KernelVersion) == {"retired-node": "previous"}


def test_collect_requires_state(tmp_path: Path) -> None:
    with ctx_state.use(None), pytest.raises(PyinfraError, match="active pyinfra state"):
        collect(server.KernelVersion)
    assert not (tmp_path / ".pyinfra-collector.sqlite3").exists()


@pytest.mark.parametrize(
    "failure",
    [
        ConnectError("offline"),
        TimeoutError("timeout"),
        OSError("offline"),
        SSHException("transport failed"),
    ],
)
def test_unreachable_hosts_are_omitted_without_replacing_stored_values(
    make_state: Callable[..., State],
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    state = make_state(limit="@fake/node0")
    original = FakeConnector.connect

    def connect(connector: FakeConnector) -> None:
        if connector.host.name == "@fake/node2":
            raise failure
        original(connector)

    monkeypatch.setattr(FakeConnector, "connect", connect)
    with FactStore() as store:
        store.record_for(server.KernelVersion, {"@fake/node2": "previous"})
    with ctx_state.use(state):
        values = collect(server.KernelVersion)
    assert values == {f"@fake/node{i}": f"6.{i}.0" for i in (0, 1, 3)}
    with FactStore() as store:
        assert store.latest("@fake/node2", "server.KernelVersion") == "previous"
    assert {host.name for host in state.active_hosts} == {"@fake/node0"}
    assert state.failed_hosts == set()
    assert {host.name for host in state.inventory if host.connected} == {"@fake/node0"}


@pytest.mark.parametrize("node", ["@fake/node0", "@fake/node2"])
@pytest.mark.parametrize(
    "failure", [TimeoutError("timeout"), OSError("offline"), SSHException("offline")]
)
def test_fact_transport_failures_do_not_change_deployment_targets(
    make_state: Callable[..., State],
    monkeypatch: pytest.MonkeyPatch,
    node: str,
    failure: Exception,
) -> None:
    state = make_state(limit="@fake/node0")
    state.config.FAIL_PERCENT = 0
    original = FakeConnector.run_shell_command

    def run(connector: FakeConnector, *args: Any, **kwargs: Any) -> Any:
        if connector.host.name == node:
            raise failure
        return original(connector, *args, **kwargs)

    monkeypatch.setattr(FakeConnector, "run_shell_command", run)
    with ctx_state.use(state):
        values = collect(server.KernelVersion)
    assert values == {
        f"@fake/node{i}": f"6.{i}.0" for i in range(4) if f"@fake/node{i}" != node
    }
    assert {host.name for host in state.active_hosts} == {"@fake/node0"}
    assert {host.name for host in state.activated_hosts} == {"@fake/node0"}
    assert state.failed_hosts == set()
    assert not Path(".pyinfra-collector.sqlite3").exists()


def test_all_selected_hosts_can_be_unreachable(
    make_state: Callable[..., State], monkeypatch: pytest.MonkeyPatch
) -> None:
    state = make_state(limit="@fake/node0")

    def connect(connector: FakeConnector) -> None:
        raise ConnectError("offline")

    monkeypatch.setattr(FakeConnector, "connect", connect)
    with ctx_state.use(state):
        assert (
            collect(
                server.KernelVersion,
                include=lambda host, fact: host.name != "@fake/node0",
            )
            == {}
        )
    assert state.failed_hosts == set()


def test_unexpected_fact_errors_propagate_after_connections_close(
    make_state: Callable[..., State], monkeypatch: pytest.MonkeyPatch
) -> None:
    state = make_state(limit="@fake/node0")

    def get_fact(host: Host, fact: type, *args: Any, **kwargs: Any) -> Any:
        gevent.sleep(0.01)
        raise ValueError("invalid fact arguments")

    monkeypatch.setattr(Host, "get_fact", get_fact)
    with (
        ctx_state.use(state),
        pytest.raises(ValueError, match="invalid fact arguments"),
    ):
        collect(server.KernelVersion)
    assert {host.name for host in state.inventory if host.connected} == {"@fake/node0"}


def test_fact_failures_caught_inside_custom_facts_are_still_omitted(
    make_state: Callable[..., State], monkeypatch: pytest.MonkeyPatch
) -> None:
    state = make_state(limit="@fake/node0")

    def get_fact(host: Host, fact: type, *args: Any, **kwargs: Any) -> Any:
        try:
            host.state.fail_hosts({host})  # type: ignore[no-untyped-call]
        except PyinfraError:
            return "fallback must not be stored"
        raise AssertionError("The private state must report its only host failed")

    monkeypatch.setattr(Host, "get_fact", get_fact)
    with ctx_state.use(state):
        assert collect(server.KernelVersion) == {}
    assert state.failed_hosts == set()


def test_unexpected_pyinfra_errors_are_not_treated_as_host_failures(
    state: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    def get_fact(host: Host, fact: type, *args: Any, **kwargs: Any) -> Any:
        raise PyinfraError("invalid fact")

    monkeypatch.setattr(Host, "get_fact", get_fact)
    with ctx_state.use(state), pytest.raises(PyinfraError, match="invalid fact"):
        collect(server.KernelVersion)


@pytest.mark.parametrize("parallel", [1, 2, 4])
def test_new_connections_fan_out_in_parallel(
    make_state: Callable[..., State], monkeypatch: pytest.MonkeyPatch, parallel: int
) -> None:
    state = make_state(parallel=parallel, limit="@fake/node0")
    original = FakeConnector.connect
    active = 0
    peak = 0

    def connect(connector: FakeConnector) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            gevent.sleep(0.01)
            original(connector)
        finally:
            active -= 1

    monkeypatch.setattr(FakeConnector, "connect", connect)
    with ctx_state.use(state):
        values = collect(
            server.KernelVersion, include=lambda host, fact: host.name != "@fake/node0"
        )
    assert len(values) == 3
    assert peak == min(parallel, 3)


def test_failed_fact_commands_are_omitted() -> None:
    inventory = Inventory(  # type: ignore[no-untyped-call]
        (
            [
                ("@fake/up", {"fake_responses": {"uname -r": "6.1.0"}}),
                (
                    "@fake/down",
                    {
                        "fake_responses": {
                            "uname -r": {
                                "success": False,
                                "stderr": "fact command failed",
                            }
                        }
                    },
                ),
            ],
            {"fake_delay": 0, "fake_delay_jitter": 0},
        )
    )
    state = State(inventory, Config())  # type: ignore[no-untyped-call]
    state.limit_hosts = [inventory.hosts["@fake/up"]]
    connect_all(state)
    with ctx_state.use(state):
        assert collect(server.KernelVersion) == {"@fake/up": "6.1.0"}
    assert state.failed_hosts == set()


def test_concurrent_requests_do_not_share_a_connection_unsafely(
    make_state: Callable[..., State], monkeypatch: pytest.MonkeyPatch
) -> None:
    state = make_state(limit="@fake/node0")
    events: list[str] = []
    original_connect = FakeConnector.connect
    original_disconnect = FakeConnector.disconnect
    original_get_fact = Host.get_fact

    def connect(connector: FakeConnector) -> None:
        events.append("connect")
        gevent.sleep(0.01)
        original_connect(connector)

    def disconnect(connector: FakeConnector) -> None:
        events.append("disconnect")
        original_disconnect(connector)

    def get_fact(host: Host, fact: type, *args: Any, **kwargs: Any) -> Any:
        events.append("fact")
        gevent.sleep(0.01)
        return original_get_fact(host, fact, *args, **kwargs)

    def include(host: Host, fact: type) -> bool:
        return host.name == "@fake/node1"

    def caller(fact: type) -> dict[str, Any]:
        with ctx_state.use(state):
            return collect(fact, include=include)

    monkeypatch.setattr(FakeConnector, "connect", connect)
    monkeypatch.setattr(FakeConnector, "disconnect", disconnect)
    monkeypatch.setattr(Host, "get_fact", get_fact)
    callers = [
        gevent.spawn(caller, fact) for fact in (server.KernelVersion, server.Hostname)
    ]
    assert all(len(caller.get(timeout=5)) == 1 for caller in callers)
    assert events == ["connect", "fact", "disconnect"] * 2
    assert not state.inventory.hosts["@fake/node1"].connected
