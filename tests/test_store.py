from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pyinfra_collector.store import FactStore


@pytest.fixture
def store(tmp_path: Path) -> FactStore:
    return FactStore(tmp_path / "facts.sqlite3")


def test_record_and_latest(store: FactStore) -> None:
    assert store.record("node1", "server.Uptime", 100)
    assert store.latest("node1", "server.Uptime") == 100


def test_latest_returns_none_when_unknown(store: FactStore) -> None:
    assert store.latest("node1", "server.Uptime") is None


def test_record_skips_unchanged_value(store: FactStore) -> None:
    assert store.record("node1", "server.Uptime", 100)
    assert not store.record("node1", "server.Uptime", 100)
    assert len(store.history("node1", "server.Uptime")) == 1


def test_record_keeps_changed_value(store: FactStore) -> None:
    store.record("node1", "server.Uptime", 100)
    assert store.record("node1", "server.Uptime", 200)
    assert store.latest("node1", "server.Uptime") == 200
    assert len(store.history("node1", "server.Uptime")) == 2


def test_record_distinguishes_args(store: FactStore) -> None:
    store.record("node1", "files.File", "a", {"args": ["/etc/hosts"], "kwargs": {}})
    store.record("node1", "files.File", "b", {"args": ["/etc/passwd"], "kwargs": {}})
    assert (
        store.latest("node1", "files.File", {"args": ["/etc/hosts"], "kwargs": {}})
        == "a"
    )
    assert (
        store.latest("node1", "files.File", {"args": ["/etc/passwd"], "kwargs": {}})
        == "b"
    )


def test_record_preserves_integer_scalars(store: FactStore) -> None:
    """A bare integer must round-trip as an integer, not as text or a corrupt value."""
    store.record("node1", "server.Uptime", 100)
    assert store.latest("node1", "server.Uptime") == 100


def test_latest_for_groups_by_node(store: FactStore) -> None:
    store.record("node1", "server.Uptime", 100)
    store.record("node2", "server.Uptime", 300)
    store.record("node1", "server.Hostname", "web1")
    assert store.latest_for("server.Uptime") == {"node1": 100, "node2": 300}


def test_latest_for_takes_newest_per_node(store: FactStore) -> None:
    store.record("node1", "server.Uptime", 100)
    store.record("node1", "server.Uptime", 200)
    assert store.latest_for("server.Uptime") == {"node1": 200}


def test_latest_for_distinguishes_args(store: FactStore) -> None:
    store.record("node1", "files.File", "a", {"args": ["/a"], "kwargs": {}})
    store.record("node1", "files.File", "b", {"args": ["/b"], "kwargs": {}})
    # The newest row overall, regardless of args.
    assert store.latest_for("files.File") == {"node1": "b"}


def test_history_is_newest_first(store: FactStore) -> None:
    store.record("node1", "server.Uptime", 100)
    store.record("node1", "server.Uptime", 200)
    values = [
        observation.data for observation in store.history("node1", "server.Uptime")
    ]
    assert values == [200, 100]


def test_history_records_timestamp_and_args(store: FactStore) -> None:
    moment = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
    store.record(
        "node1", "files.File", "a", {"args": ["/a"], "kwargs": {}}, collected_at=moment
    )
    (observation,) = store.history(
        "node1", "files.File", {"args": ["/a"], "kwargs": {}}
    )
    assert observation.collected_at == moment
    assert observation.args == {"args": ["/a"], "kwargs": {}}
    assert observation.node == "node1"
    assert observation.fact == "files.File"
    assert observation.key[0] == "node1"


def test_prune_everything(store: FactStore) -> None:
    store.record("node1", "server.Uptime", 100)
    store.record("node1", "server.Uptime", 200)
    assert store.prune() == 2
    assert store.history("node1", "server.Uptime") == []


def test_prune_keep_retains_newest_per_key(store: FactStore) -> None:
    store.record("node1", "server.Uptime", 100)
    store.record("node1", "server.Uptime", 200)
    store.record("node1", "server.Uptime", 300)
    assert store.prune(keep=1) == 2
    assert [o.data for o in store.history("node1", "server.Uptime")] == [300]


def test_prune_keep_per_key_independent(store: FactStore) -> None:
    store.record("node1", "server.Uptime", 100)
    store.record("node1", "server.Uptime", 200)
    store.record("node2", "server.Uptime", 300)
    assert store.prune(keep=1) == 1
    assert store.latest("node1", "server.Uptime") == 200
    assert store.latest("node2", "server.Uptime") == 300


def test_prune_older_than(store: FactStore) -> None:
    old = datetime.now(UTC) - timedelta(days=10)
    store.record("node1", "server.Uptime", 100, collected_at=old)
    store.record("node1", "server.Uptime", 200)
    assert store.prune(older_than=timedelta(days=1)) == 1
    assert [o.data for o in store.history("node1", "server.Uptime")] == [200]


def test_prune_older_than_combined_with_keep(store: FactStore) -> None:
    old = datetime.now(UTC) - timedelta(days=10)
    store.record("node1", "server.Uptime", 100, collected_at=old)
    store.record("node1", "server.Uptime", 200, collected_at=old)
    store.record("node1", "server.Uptime", 300)
    # Both old rows qualify, but keep=2 means only one may go.
    assert store.prune(keep=2, older_than=timedelta(days=1)) == 1
    assert [o.data for o in store.history("node1", "server.Uptime")] == [300, 200]


def test_store_is_reopenable(tmp_path: Path) -> None:
    path = tmp_path / "facts.sqlite3"
    with FactStore(path) as store:
        store.record("node1", "server.Uptime", 100)
    with FactStore(path) as store:
        assert store.latest("node1", "server.Uptime") == 100


def test_repeated_open_is_idempotent(tmp_path: Path) -> None:
    """Re-opening must not fail on the already-existing schema."""
    path = tmp_path / "facts.sqlite3"
    FactStore(path).close()
    FactStore(path).close()


def test_record_rolls_back_on_failure(
    store: FactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure while writing must not leave a half-committed row behind."""
    import pyinfra_collector.store as store_module

    calls = 0
    original = store_module._dumps

    def flaky(value: object) -> str:
        nonlocal calls
        calls += 1
        # First call is the payload; second is the args inside the INSERT.
        if calls == 2:
            raise RuntimeError("boom")
        return original(value)

    monkeypatch.setattr(store_module, "_dumps", flaky)
    with pytest.raises(RuntimeError, match="boom"):
        store.record("node1", "server.Uptime", 100)
    monkeypatch.undo()
    assert store.history("node1", "server.Uptime") == []
