# pyinfra-collector

Collect [pyinfra](https://pyinfra.com) facts across the entire inventory.
Use the returned mapping in the same deploy. Optionally persist it with
`FactStore` to read the values in a later invocation.

## Collect and use a fact

`collect(fact, *args, include=..., **kwargs)` collects one fact across the
inventory, regardless of `--limit`. It returns a mapping of successful host
names to values without reading or writing storage. Collection happens during
deploy preparation, before the caller adds operations that use the mapping.
No database is opened or created unless the deploy explicitly uses `FactStore`.

For example, `publish_kernels.py` collects kernel versions and passes the
complete mapping to a template:

```python
from pyinfra.facts import server
from pyinfra.operations import files

from pyinfra_collector import collect

kernels = collect(server.KernelVersion)

files.template(
    src="kernels.j2",
    dest="/var/tmp/fleet-kernels",
    kernels=kernels,
)
```

The template, `kernels.j2`, contains:

```jinja2
{% for node, version in kernels.items() %}
{{ node }} {{ version }}
{% endfor %}
```

Run the deploy with your inventory:

```sh
pyinfra inventory.py publish_kernels.py
```

Each collection call declares its complete request. For multiple facts, call
`collect()` for each fact. Pass fact arguments as normal positional or keyword
arguments, such as `collect(files.File, "/etc/example.conf")`, where `files` is
`pyinfra.facts.files`.

### Parallel collection and shared results

Collection uses pyinfra's host connectors and `state.fact_pool`. It reuses
existing connections and connects other selected hosts through pyinfra.
Connections opened only for collection close after use. Deployment connections
stay open for pyinfra to manage.

Connection and fact work run concurrently up to pyinfra's `PARALLEL` limit.
A capped pool processes hosts in waves. With `PARALLEL=1`, collection is serial.
Concurrent requests to the same host use its connector one at a time.

Equivalent calls in one pyinfra state share a single collection. Calls are
equivalent when their fact id, arguments, and `include` predicate object match.
The first caller collects the values. Concurrent callers wait for that result.
Later callers reuse it. Each caller receives an independent copy.

This cache belongs to the collector. Native `host.get_fact()` calls do not
reuse it. With pyinfra 3.10.0, those calls fetch the fact again. Use equivalent
`collect()` calls to share the mapping between deploys in one state.

Callers do not wait for other hosts to reach `collect()`. The call also works
inside a deploy branch that only some hosts execute. No barrier or lifecycle
callback is needed, and `PARALLEL` can be smaller than the inventory.

Unavailable hosts are omitted, as described below. Unexpected collection
errors raise for every caller. Later calls for that request in the same state
also raise rather than retrying. Explicit storage failures do not change the
cached collection result.

### Collection output

Normal output reports when each fact collection starts and finishes. The
completion message gives the number of successful hosts and selected hosts:

```text
--> Collecting server.KernelVersion from hosts...
--> Collected server.KernelVersion from 4/4 selected hosts.
```

With `-v` or higher verbosity, each successful host also reports the available
fact with pyinfra's host prefix:

```text
    [lotus.rw.madduck.net] Available fact: server.KernelVersion
```

Collector messages do not include fact values or arguments. Pyinfra's own
verbose output can include fact arguments, commands, and command output.
Excluded hosts do not count as selected. Hosts with connection or fact failures
count as selected but not successful. Unexpected errors raise without a
completion message.

Messages appear during operation preparation, when the deploy calls
`collect()`, not before pyinfra's preparation heading. Separate requests report
separately. Calls that reuse a shared result do not repeat the messages.
`FactCollector.run()` reports each queued collection in the same way.

### Collection scope

`collect()` applies its predicate to every host in the inventory. Only matching
hosts enter the collection pool. `--limit` restricts deployment operations,
not collection. Connecting a host for collection does not add it to the
deployment targets.

For example, this command collects across the inventory but renders and writes
the kernel list only on `publisher`:

```sh
pyinfra inventory.py publish_kernels.py --limit publisher
```

A rejected host needs no connection for collection. Pyinfra can still connect
that host independently if it is a deployment target.

### Filtering hosts

The `include` predicate receives the host and the fact class. Only hosts for
which it returns true contribute. Excluded hosts are not contacted for the
requested fact. Define a shared predicate in `shared_predicates.py`:

```python
from pyinfra.api.host import Host

from pyinfra_collector.typedefs import FactClass


def web_hosts(host: Host, fact: FactClass) -> bool:
    """Return whether the host belongs to the web group."""
    return "web" in host.groups
```

Import that predicate into the deploy:

```python
from pyinfra.facts import server
from shared_predicates import web_hosts

from pyinfra_collector import collect

kernels = collect(server.KernelVersion, include=web_hosts)
```

Predicates run before collection connection work starts. A predicate must use
local metadata, such as the host name, groups, or inventory data. It must not
read remote facts or require a connection. This rule also applies when the
host already has a connection for deployment.

Reuse the same predicate object when calls must share a filtered collection.
Deploy files run separately for each host, so functions defined in those files
are different objects. Put shared predicates in an imported Python module.

### Missing hosts and values

A host that cannot connect, times out, or fails to collect its fact is absent
from the returned mapping. Other selected hosts still contribute. Native fact
failures remain local to collection and do not mark deployment targets failed.
Invalid fact arguments and other unexpected errors still raise.

Collection does not fill missing values from SQLite. It also does not write
a default value or delete existing observations for an unavailable host.
The caller decides whether to use old values, reject incomplete results, or
change the stored observations. Successful `None` values remain in the mapping.

Equivalent calls in one state reuse the same result, including omissions.
They do not retry an unavailable host during that state.

## Persist values between invocations

Collection does not persist automatically. To save values for another run,
write them explicitly with `FactStore.record_for()`. The collection deploy,
`collect_facts.py`, contains:

```python
from pyinfra.facts import server

from pyinfra_collector import FactStore, collect

kernels = collect(server.KernelVersion)
with FactStore() as store:
    changed = store.record_for(server.KernelVersion, kernels)
```

`record_for()` accepts a mapping of node names to values and returns the number
of new or changed observations. It saves only supplied nodes. Omitted nodes
retain their previous observations. An empty mapping changes nothing.

The publishing deploy, `publish.py`, reads the store without collecting again:

```python
from pyinfra.facts import server
from pyinfra.operations import files

from pyinfra_collector import FactStore

with FactStore() as store:
    kernels = store.latest_for(server.KernelVersion)

files.template(
    src="kernels.j2",
    dest="/var/tmp/fleet-kernels",
    kernels=kernels,
)
```

Run collection first. Run publishing only if collection succeeds. Use the same
working directory for both invocations so they share the default database:

```sh
pyinfra inventory.py collect_facts.py && pyinfra inventory.py publish.py
```

Collection runs during preparation, including dry runs and debug preparation.
It does not access SQLite. If a deploy explicitly calls `record_for()` during
preparation, that call writes to the controller-local store even in a dry run.

## The store

The store is an append-only log of observations, keyed by node, fact, and fact
arguments. A new row is written only when the value changes.

`FactStore` works independently of collection. It needs no active pyinfra state
or host connections. Opening it creates the database if needed. The default
path is `.pyinfra-collector.sqlite3` in the current directory.

The schema stores observation times in a `TIMESTAMP` column and arguments and
values as JSON text.

`FactStore` offers these methods:

- `record_for(fact, values, *args, **kwargs)` records a node-to-value mapping.
  It accepts a fact class or canonical id and returns the changed row count.
- `latest(node, fact, *args, **kwargs)` returns the newest value for one node
  and canonical fact id, or `None` if no value exists.
- `latest_for(fact, *args, **kwargs)` returns the newest value per node that
  ever reported that fact. It accepts a fact class or its canonical id.
- `history(node, fact, *args, **kwargs)` returns observations newest first.
- `prune(keep=..., older_than=...)` deletes old observations. `keep` retains the
  newest rows per fact tuple. `older_than` restricts deletion by age.

Pass arguments in the same form used for collection. Positional and keyword
forms identify different requests. `prune()` with no arguments deletes all
observations.

`latest_for()` includes historical hosts, even outside the current inventory.
It does not prove that a recent run collected every intended host. Unchanged
values do not create new observations, so their timestamps are not freshness
markers.

### Explicit queues for Python callers

Construct `FactCollector()` without a store. Its `gather()` method queues
requests without contacting hosts. With a pyinfra state context bound, `run()`
returns `(request, values)` pairs in queue order. Each request retains its fact
class, positional arguments, and keyword arguments for optional persistence.
No storage is read or written.

Requests remain pending until `reset()` clears the queue. Duplicate fact ids
and arguments retain the first request and its predicate.

This queue does not coordinate per-host deploy loading. Use module-level
`collect(fact, ...)` inside deploys instead.

## Migration

Replace module-level `gather(fact, ...)` followed by `collect()` with
`collect(fact, ...)`. The call returns the mapping directly, so a following
store read is unnecessary for same-run use. Module-level `gather` and
`PrepareEndFlush` are removed. There is no dependency on an unreleased pyinfra
callback.

Predicates must now use local metadata only. Collection covers the entire
inventory even with `--limit`. Unavailable hosts are absent from the returned
mapping, and collection does not access stored observations.

Persistence is now explicit. Pass the returned mapping to
`store.record_for(fact, values, *args, **kwargs)` to save it. `FactCollector()`
no longer takes a store, and `run()` returns request/result pairs rather than
a write count.

## Limitations

- The store is local to the controller. It is not shared between controllers or
  distributed to nodes.
- Equivalent requests reuse one result per state. Repeated calls do not refresh
  changed remote values or retry missing hosts.
- Explicit store writes are serial and commit one observation at a time.
  A write failure can leave some observations committed. The collection result
  remains available for the caller to retry persistence.

## Legalese

`pyinfra-collector` is © 2026 martin f. krafft
<pyinfra-collector@pobox.madduck.net>.

It is released under the terms of the MIT Licence.
