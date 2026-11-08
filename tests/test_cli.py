import json
from pathlib import Path
from typing import Any

import gevent
import pyinfra
import pytest
from click.testing import CliRunner
from pyinfra.api.exceptions import ConnectError
from pyinfra.connectors.fake import FakeConnector
from pyinfra.context import ctx_host, ctx_state
from pyinfra_cli.cli import cli


@pytest.fixture
def deploy_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(pyinfra, "is_cli", True)
    monkeypatch.setattr(pyinfra.logger, "handlers", pyinfra.logger.handlers.copy())
    monkeypatch.setattr(pyinfra.logger, "level", pyinfra.logger.level)
    (tmp_path / "inventory.py").write_text(
        "import re\n"
        "nodes = [\n"
        "    (f'@fake/node{i}', {\n"
        "        'fake_delay': 0,\n"
        "        'fake_delay_jitter': 0,\n"
        "        'fake_responses': {re.compile(r'uname -r'): f'6.{i}.0'},\n"
        "    }) for i in range(4)\n"
        "]\n"
    )
    (tmp_path / "collect.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "from pyinfra.facts import server\n"
        "from pyinfra_collector import collect\n"
        "values = collect(server.KernelVersion)\n"
        "Path('collected.json').write_text(json.dumps(values))\n"
    )
    (tmp_path / "persist.py").write_text(
        "from pyinfra.facts import server\n"
        "from pyinfra_collector import FactStore, collect\n"
        "values = collect(server.KernelVersion)\n"
        "with FactStore() as store:\n"
        "    store.record_for(server.KernelVersion, values)\n"
    )
    (tmp_path / "publish.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "from pyinfra import host\n"
        "from pyinfra.facts import server\n"
        "from pyinfra_collector import FactStore\n"
        "with FactStore() as store:\n"
        "    values = store.latest_for(server.KernelVersion)\n"
        "Path(host.name.replace('/', '_') + '.json').write_text(json.dumps(values))\n"
    )
    return tmp_path


@pytest.mark.parametrize("flag", ["--dry", "--debug-facts", "--debug-operations"])
def test_cli_collects_during_preparation(deploy_files: Path, flag: str) -> None:
    with ctx_state.use(None), ctx_host.use(None), gevent.Timeout(5):
        result = CliRunner().invoke(
            cli, ["inventory.py", "collect.py", "--parallel", "1", flag]
        )
    assert result.exit_code == 0, result.output
    assert json.loads((deploy_files / "collected.json").read_text()) == {
        f"@fake/node{i}": f"6.{i}.0" for i in range(4)
    }
    assert not (deploy_files / ".pyinfra-collector.sqlite3").exists()


@pytest.mark.parametrize("parallel", [1, 2, 4])
@pytest.mark.parametrize("failure", [None, "connect", "fact", "target_fact"])
def test_cli_limit_restricts_operations_not_collection(
    deploy_files: Path,
    parallel: int,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
) -> None:
    commands: dict[str, list[str]] = {}
    original = FakeConnector.run_shell_command
    original_connect = FakeConnector.connect
    missing = None if failure is None else (0 if failure == "target_fact" else 2)

    def connect(connector: FakeConnector) -> None:
        if failure == "connect" and connector.host.name == "@fake/node2":
            raise ConnectError("offline")
        original_connect(connector)

    def run(connector: FakeConnector, command: Any, **kwargs: Any) -> Any:
        commands.setdefault(connector.host.name, []).append(str(command))
        if (
            failure in {"fact", "target_fact"}
            and connector.host.name == f"@fake/node{missing}"
            and str(command) == "uname -r"
        ):
            raise TimeoutError("fact timed out")
        return original(connector, command, **kwargs)

    monkeypatch.setattr(FakeConnector, "connect", connect)
    monkeypatch.setattr(FakeConnector, "run_shell_command", run)

    (deploy_files / "limited.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "from pyinfra import host\n"
        "from pyinfra.facts import server\n"
        "from pyinfra.operations import server as operations\n"
        "from pyinfra_collector import collect\n"
        "values = collect(server.KernelVersion)\n"
        "Path(host.name.replace('/', '_') + '.json').write_text(json.dumps(values))\n"
        "operations.shell(commands='echo deploy-only')\n"
    )
    with ctx_state.use(None), ctx_host.use(None), gevent.Timeout(5):
        result = CliRunner().invoke(
            cli,
            [
                "inventory.py",
                "limited.py",
                "--limit",
                "@fake/node0",
                "--parallel",
                str(parallel),
                "--fail-percent",
                "0",
                "--yes",
            ],
        )
    assert result.exit_code == 0, result.output
    assert json.loads((deploy_files / "@fake_node0.json").read_text()) == {
        f"@fake/node{i}": f"6.{i}.0" for i in range(4) if i != missing
    }
    assert not (deploy_files / "@fake_node1.json").exists()
    assert set(commands) == {
        f"@fake/node{i}" for i in range(4) if not (failure == "connect" and i == 2)
    }
    assert "echo deploy-only" in commands["@fake/node0"]
    assert all(
        "echo deploy-only" not in commands.get(f"@fake/node{i}", [])
        for i in range(1, 4)
    )


def test_cli_collect_then_publish_in_limited_invocation(deploy_files: Path) -> None:
    runner = CliRunner()
    with ctx_state.use(None), ctx_host.use(None), gevent.Timeout(5):
        collected = runner.invoke(
            cli, ["inventory.py", "persist.py", "--parallel", "2", "--dry"]
        )
    assert collected.exit_code == 0, collected.output
    with ctx_state.use(None), ctx_host.use(None), gevent.Timeout(5):
        published = runner.invoke(
            cli, ["inventory.py", "publish.py", "--limit", "@fake/node0", "--dry"]
        )
    assert published.exit_code == 0, published.output
    assert json.loads((deploy_files / "@fake_node0.json").read_text()) == {
        f"@fake/node{i}": f"6.{i}.0" for i in range(4)
    }
    assert not (deploy_files / "@fake_node1.json").exists()
