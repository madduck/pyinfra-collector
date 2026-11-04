# pyinfra-collector

Resources for use with [pyinfra](https://pyinfra.com) that collect facts from
the whole inventory into a local store.

It's all work-in-progress, expect more resources and docs soon.

## Collecting facts across the inventory

A pyinfra fact is gathered from one host at a time. Sometimes a deploy needs the
values from *every* host on the controller side, for example to fill one
template with the uptime of the whole fleet.

The `pyinfra_collector.collector` module gathers such values once and keeps them
in a local SQLite store, where every deployment phase can read them back.

### How it works

Collection happens in two steps, because gathering is not a per-host operation:

1. `register()` records the facts to collect. It does not touch any host.
2. The `collect()` deploy gathers everything registered, once per run, and writes
   it to the store.

This split means a deploy can register facts as it goes and does not need to
know the full list up front. Registering the same fact twice is a no-op.

### Example

```python
from pyinfra.facts import server
from pyinfra.operations import files
from pyinfra.api.deploy import deploy

from pyinfra_collector.collector import collect, register
from pyinfra_collector.store import FactStore


@deploy()
def gather() -> None:
    register(server.Uptime)
    collect()


@deploy()
def publish() -> None:
    with FactStore() as store:
        uptimes = store.latest_for("server.Uptime")

    files.template(
        src="uptimes.j2",
        dest="/var/cache/pyinfra-collector/uptimes",
        uptimes=uptimes,
    )
```

Run `gather` and `publish` as separate pyinfra invocations. `uptimes` is then a
mapping of node name to the latest collected value, ready for a template or for
looping over on the controller.

### Filtering hosts

`register()` takes an `include` predicate. A host contributes only when the
predicate returns true. The predicate receives the host and the fact class, and
may inspect the host object, its groups and data, or gather another fact:

```python
register(server.Uptime, include=lambda host, fact: "web" in host.groups)
```

### The store

The store is an append-only log of observations, keyed by node, fact, and the
arguments the fact was called with. A new row is written only when the value
changes, so repeatedly collecting an unchanged fact does not grow the store.

The database lives at `.pyinfra-collector.sqlite3` in the current directory. The
schema uses a `TIMESTAMP` column for the collection time and stores the fact
arguments and values as JSON text.

`FactStore` offers:

- `latest(node, fact, args)` — the newest value for one node and fact.
- `latest_for(fact)` — a mapping of node name to newest value, for every node
  that ever reported the fact. `fact` is a fact class or its canonical id,
  e.g. `server.Uptime`.
- `history(node, fact, args)` — every observation, newest first.
- `record(...)` — append one observation, skipping it if unchanged.
- `prune(keep=..., older_than=...)` — delete old observations. `keep` retains
  the newest rows per fact tuple; `older_than` limits deletion to rows older
  than a duration. Call this from a teardown step.

`prune()` with no arguments deletes everything.

### Limitations

- The store is controller-local state. It is not shared between controllers and
  is not distributed to the nodes.
- Values are gathered at collection time and may be stale by the time they are
  read. Read them in a phase that runs after the values could change, or collect
  again.
- Hosts are gathered in parallel through pyinfra's fact pool. Usable
  parallelism follows pyinfra's `PARALLEL` configuration.
- Writing to the store is serial, one row at a time.

## Legalese

`pyinfra-collector` is © 2026 martin f. krafft
<pyinfra-collector@pobox.madduck.net>.

It is released under the terms of the MIT Licence.
