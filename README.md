# pyinfra-collector

Resources for use with [pyinfra](https://pyinfra.com) that collect facts from
the whole inventory into a local store.

It's all work-in-progress, expect more resources and docs soon.

## Collecting facts across the inventory

A pyinfra fact is gathered from one host at a time. Sometimes a deploy needs the
values from _every_ host on the controller side, for example to fill one
template with the uptime of the whole fleet.

The `pyinfra_collector.collector` module gathers such values once and keeps them
in a local SQLite store, where every deployment phase can read them back.

### How it works

Collection happens in two steps, because gathering is not a per-host operation:

1. `gather()` adds the facts to collect. It does not touch any host.
2. pyinfra collects everything gathered at the end of the prepare phase and
   writes it to the store.

This split means a deploy can gather facts as it goes and does not need to know
the full list up front. Gathering the same fact twice is a no-op.

Step 2 is meant to happen automatically. The first `gather()` call arms a callback
on the run, and pyinfra fires it once when the prepare phase ends, before any
operation executes. You then do not call anything to make collection happen.

Collection is read-only, so on a pyinfra that fires the callback it also runs for
a dry run (`--dry`) and for debug dumps (`--debug-facts`, `--debug-operations`).
Those runs still fill the store.

#### Automatic collection needs pyinfra PR #2063

That callback is the `prepare_end` hook, which is not in a released pyinfra yet.
It is proposed in [pyinfra PR #2063][pr2063] (`feat(api): add prepare_end
callback`). Until that PR lands in a release, no released pyinfra fires
`prepare_end`, so the `gather()` call arms a callback that never runs and
**nothing is collected**.

[pr2063]: https://github.com/pyinfra-dev/pyinfra/pull/2063

#### What is required today

On a released pyinfra you must trigger collection yourself by calling the
`collect()` deploy at the end of the deploy that gathered the facts. It flushes
whatever `gather()` accumulated, once per run, before any operation executes:

```python
from pyinfra.facts import server
from pyinfra.api.deploy import deploy

from pyinfra_collector.collector import collect, gather


@deploy()
def gather_facts() -> None:
    gather(server.Uptime)
    collect()
```

Call `collect()` after the last `gather()` in the deploy, so it flushes the full
set. Calling it more than once in a run does nothing extra.

A deploy file runs once per active host, in parallel, and every host reaches
`collect()`. Because collection is a whole-inventory operation, those calls act
as a barrier: every host waits until the last one has arrived, then exactly one
flushes the collector and the rest resume. That keeps a host from reading the
store before the flush commits, and makes sure the flush covers every host's
`gather()` calls. It is therefore safe to gather, collect, and read the store in
the same deploy file.

Every active host must reach `collect()`. A deploy that calls it for only some
of the active hosts leaves the barrier waiting forever. A module-level call is
reached by every active host, which is the intended use.

The barrier also needs **every host's deploy run to be in flight at the same
time**, so pyinfra's `PARALLEL` must be at least the number of active hosts.
With the default `PARALLEL`, pyinfra sizes the pool to the inventory, so this
holds. If you set `PARALLEL` below the host count, pyinfra can only load a
subset of hosts at once; the loaded hosts block in the barrier waiting for the
hosts that cannot start, and the run deadlocks. Either leave `PARALLEL` at its
default, set it to at least the host count, or split the work into separate
invocations so that gathering and reading happen in different runs.

`collect()` is harmless on a pyinfra that fires `prepare_end`: the callback
flushes the collector first, so the later `collect()` call finds nothing pending
and returns. This means the same deploy works both with and without PR #2063.

### Example

This is silly, but you get the point:

```python
from pyinfra.facts import server
from pyinfra.operations import files
from pyinfra.api.deploy import deploy

from pyinfra_collector.collector import gather
from pyinfra_collector.store import FactStore


@deploy()
def gather_facts() -> None:
    gather(server.Uptime)


@deploy()
def publish() -> None:
    with FactStore() as store:
        uptimes = store.latest_for(server.Uptime)

    files.template(
        src="uptimes.j2",
        dest="/var/tmp/uptimes",
        uptimes=uptimes,
    )
```

Run `gather_facts` and `publish` as separate pyinfra invocations. Without the
prepare-end PR, add `collect()` to the end of `gather_facts` as shown above, or
the fact is never collected. With it, `gather()` alone is enough and `collect()`
is redundant. `uptimes` is then a mapping of node name to the latest collected
value, ready for a template or for looping over on the controller.

### Filtering hosts

`gather()` takes an `include` predicate. A host contributes only when the
predicate returns true. The predicate receives the host and the fact class, and
may inspect the host object, its groups and data, or gather another fact:

```python
gather(server.Uptime, include=lambda host, fact: "web" in host.groups)
```

### The store

The store is an append-only log of observations, keyed by node, fact, and the
arguments the fact was called with. A new row is written only when the value
changes, so repeatedly collecting an unchanged fact does not grow the store.

The database lives at `.pyinfra-collector.sqlite3` in the current directory. The
schema uses a `TIMESTAMP` column for the collection time and stores the fact
arguments and values as JSON text.

`FactStore` offers:

- `latest(node, fact, *args, **kwargs)` — the newest value for one node and
  fact.
- `latest_for(fact, *args, **kwargs)` — a mapping of node name to newest value,
  for every node that ever reported the fact with those arguments. `fact` is a
  fact class or its canonical id, e.g. `server.Uptime`.
- `history(node, fact, *args, **kwargs)` — every observation, newest first.
- `_record(...)` — append one observation, skipping it if unchanged. Internal;
the collector calls it.
- `prune(keep=..., older_than=...)` — delete old observations. `keep` retains
  the newest rows per fact tuple; `older_than` limits deletion to rows older
  than a duration. Call this from a teardown step.

Pass a fact's arguments the same way you pass them to `gather()`: as normal
positional and keyword arguments. Leave them out for a fact that takes none.

`prune()` with no arguments deletes everything.

### Limitations

- The store is controller-local state. It is not shared between controllers and
  is not distributed to the nodes.
- Values are gathered at collection time and may be stale by the time they are
  read. Read them in a phase that runs after the values could change, or collect
  again.
- Hosts are gathered in parallel through pyinfra's fact pool. Usable parallelism
  follows pyinfra's `PARALLEL` configuration.
- Writing to the store is serial, one row at a time.

## Legalese

`pyinfra-collector` is © 2026 martin f. krafft
<pyinfra-collector@pobox.madduck.net>.

It is released under the terms of the MIT Licence.
