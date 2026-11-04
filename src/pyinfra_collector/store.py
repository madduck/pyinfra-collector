"""Persistence for collected facts.

Collecting a fact from every host produces a stream of observations, not a single
value. This module keeps those observations as an append-only log and exposes the
latest observation per ``(node, fact, args)`` tuple.

The store is a SQLite database on the controller. The database file lives next to
the deploy and is not part of the repository.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .typedefs import FactClass, args_data, fact_id

DEFAULT_NAME = ".pyinfra-collector.sqlite3"


def default_path() -> Path:
    """Return the default store location, resolved against the current directory.

    Resolved on call, not at import, so the store follows the working directory
    of the deploy that opens it.
    """
    return Path.cwd() / DEFAULT_NAME


# `args` and `data` hold JSON text (json_extract() works on them regardless).
# They must not be declared JSON: SQLite gives an unknown type name NUMERIC
# affinity, which silently converts the text '100' to the integer 100. That
# would corrupt collected scalars and break change detection. TEXT affinity
# stores them verbatim. The table is not STRICT either, since STRICT allows only
# the six base type names and rejects TIMESTAMP.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id           INTEGER   PRIMARY KEY AUTOINCREMENT,
    collected_at TIMESTAMP NOT NULL,
    node         TEXT      NOT NULL,
    fact         TEXT      NOT NULL,
    args         TEXT      NOT NULL,
    data         TEXT      NOT NULL
);

CREATE INDEX IF NOT EXISTS facts_latest
    ON facts (node, fact, args, collected_at DESC, id DESC);
"""


@dataclass(frozen=True)
class Observation:
    """One collected observation of a fact.

    Attributes:
        node: Name of the node the fact was collected from.
        fact: Canonical fact id, e.g. ``server.Uptime``.
        args: Bound arguments the fact was called with, as JSON-compatible data.
        data: The collected value, as JSON-compatible data.
        collected_at: Time of collection, in UTC.
    """

    node: str
    fact: str
    args: dict[str, Any]
    data: Any
    collected_at: datetime

    @property
    def key(self) -> tuple[str, str, str]:
        """Return the tuple identifying the fact, ignoring the value."""
        return (self.node, self.fact, _dumps(self.args))


def _dumps(value: Any) -> str:
    """Serialise a value to canonical JSON, so equal values compare equal."""
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def _args_json(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """Serialise bound fact arguments to the canonical stored form."""
    return _dumps(args_data(args, kwargs))


def _now() -> datetime:
    """Return the current time with sub-second precision, in UTC."""
    return datetime.now(UTC)


def _format_time(moment: datetime) -> str:
    """Return `moment` as an ISO-8601 string, keeping microseconds."""
    return moment.isoformat(timespec="microseconds")


class FactStore:
    """An append-only store of collected facts, backed by SQLite.

    The store only writes a row when the value differs from the latest value for
    the same ``(node, fact, args)`` tuple. Repeated identical collections are
    therefore free of growth.
    """

    def __init__(self, path: Path | None = None) -> None:
        """Open the store, creating the database and schema if needed.

        Args:
            path: Location of the SQLite database file. Defaults to
                :func:`default_path`.
        """
        path = default_path() if path is None else path
        self.path = path
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        """Close the underlying database connection."""
        self._conn.close()

    def __enter__(self) -> FactStore:
        """Enter a context that closes the store on exit."""
        return self

    def __exit__(self, *_: object) -> None:
        """Close the store."""
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block in an immediate transaction, committing on success."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.rollback()
            raise
        self._conn.commit()

    def latest(
        self,
        node: str,
        fact: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Return the latest value of one fact, or `None` if never collected.

        Args:
            node: Name of the node.
            fact: Canonical fact id.
            *args: Fact arguments, matching the call that collected it.
            **kwargs: Fact keyword arguments, matching the collection call.

        Returns:
            The most recently collected value, or `None`.
        """
        row = self._conn.execute(
            """
            SELECT data FROM facts
            WHERE node = ? AND fact = ? AND args = ?
            ORDER BY collected_at DESC, id DESC
            LIMIT 1
            """,
            (node, fact, _args_json(args, kwargs)),
        ).fetchone()
        return None if row is None else json.loads(row["data"])

    def _record(
        self,
        node: str,
        fact: str,
        data: Any,
        args: dict[str, Any] | None = None,
        collected_at: datetime | None = None,
    ) -> bool:
        """Record one observation, skipping it if it repeats the latest value.

        Args:
            node: Name of the node the fact was collected from.
            fact: Canonical fact id.
            data: The collected value, as JSON-compatible data.
            args: Bound arguments the fact was called with.
            collected_at: Time of collection, defaults to now.

        Returns:
            `True` if a new row was inserted, `False` if the value was unchanged.
        """
        payload = _dumps(data)
        with self._transaction() as conn:
            # Built inside the transaction so a serialisation failure rolls back
            # through the same path as a write failure.
            bound = _dumps(args) if args is not None else _args_json((), {})
            previous = conn.execute(
                """
                SELECT data FROM facts
                WHERE node = ? AND fact = ? AND args = ?
                ORDER BY collected_at DESC, id DESC
                LIMIT 1
                """,
                (node, fact, bound),
            ).fetchone()
            if previous is not None and previous["data"] == payload:
                return False
            conn.execute(
                "INSERT INTO facts (collected_at, node, fact, args, data)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    _format_time(collected_at or _now()),
                    node,
                    fact,
                    bound,
                    payload,
                ),
            )
        return True

    def latest_for(
        self,
        fact: str | FactClass,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Return the latest value of one fact for every node that reported it.

        Args:
            fact: A fact class, or its canonical fact id such as
                ``server.Uptime``.
            *args: Fact arguments, matching the call that collected it. A fact
                collected with different arguments yields one value per argument
                set, so the arguments select which set to read.
            **kwargs: Fact keyword arguments, matching the collection call.

        Returns:
            A mapping of node name to the latest value. Nodes that never reported
            the fact with these arguments are absent.
        """
        fact = fact_id(fact)
        rows = self._conn.execute(
            """
            SELECT node, data
            FROM (
                SELECT
                    node,
                    data,
                    ROW_NUMBER() OVER (
                        PARTITION BY node
                        ORDER BY collected_at DESC, id DESC
                    ) AS rn
                FROM facts
                WHERE fact = ? AND args = ?
            )
            WHERE rn = 1
            ORDER BY node
            """,
            (fact, _args_json(args, kwargs)),
        ).fetchall()
        return {row["node"]: json.loads(row["data"]) for row in rows}

    def history(
        self,
        node: str,
        fact: str,
        *args: Any,
        **kwargs: Any,
    ) -> list[Observation]:
        """Return every recorded observation of one fact, newest first.

        Args:
            node: Name of the node.
            fact: Canonical fact id.
            *args: Fact arguments, matching the call that collected it.
            **kwargs: Fact keyword arguments, matching the collection call.

        Returns:
            The observations, most recent first.
        """
        rows = self._conn.execute(
            """
            SELECT collected_at, node, fact, args, data FROM facts
            WHERE node = ? AND fact = ? AND args = ?
            ORDER BY collected_at DESC, id DESC
            """,
            (node, fact, _args_json(args, kwargs)),
        ).fetchall()
        return [
            Observation(
                node=row["node"],
                fact=row["fact"],
                args=json.loads(row["args"]),
                data=json.loads(row["data"]),
                collected_at=datetime.fromisoformat(row["collected_at"]),
            )
            for row in rows
        ]

    def prune(
        self,
        keep: int | None = None,
        older_than: timedelta | None = None,
    ) -> int:
        """Delete old observations.

        With neither argument, everything is deleted. With `keep`, the `keep`
        newest rows per ``(node, fact, args)`` tuple are retained. With
        `older_than`, only rows collected before the cutoff may be deleted.
        Both arguments may be combined: a row is deleted only when it is older
        than the cutoff *and* not among the `keep` newest of its tuple.

        Args:
            keep: Number of newest observations to retain per fact tuple.
            older_than: Only prune observations older than this duration.

        Returns:
            The number of rows deleted.
        """
        with self._transaction() as conn:
            if keep is None:
                if older_than is None:
                    return conn.execute("DELETE FROM facts").rowcount
                age_cutoff = _format_time(_now() - older_than)
                return conn.execute(
                    "DELETE FROM facts WHERE collected_at < ?", (age_cutoff,)
                ).rowcount

            cutoff: str | None = (
                None if older_than is None else _format_time(_now() - older_than)
            )

            delete = """
                DELETE FROM facts WHERE rowid IN (
                    SELECT rowid FROM (
                        SELECT
                            rowid,
                            collected_at,
                            ROW_NUMBER() OVER (
                                PARTITION BY node, fact, args
                                ORDER BY collected_at DESC, id DESC
                            ) AS rn
                        FROM facts
                    )
                    WHERE rn > ?
            """
            params: list[Any] = [keep]
            if cutoff is not None:
                delete += " AND collected_at < ?"
                params.append(cutoff)
            delete += ")"

            return conn.execute(delete, params).rowcount
