# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `FactStore.record_for(fact, values, *args, **kwargs)` explicitly persists a
  node-to-value mapping and returns the number of new or changed observations.
  It works without a pyinfra state and leaves omitted nodes unchanged.

### Changed

- `collect(fact, *args, include=..., **kwargs)` collects an explicit request
  during preparation and returns successfully collected inventory values.
  It uses pyinfra's fact pool and host connectors, with concurrency bounded by
  `PARALLEL`. It no longer opens SQLite or persists values automatically.
- Equivalent collection calls share their result or failure within one pyinfra
  state. Each caller receives an independent copy. Different arguments or
  predicate objects identify separate collections.
- `collect()` iterates the entire inventory, regardless of `--limit`, and
  schedules only hosts accepted by the predicate. Predicates must use local
  metadata and must not require a connection or read remote facts.
- Collection reuses existing connections and opens others through pyinfra as
  needed. Connections opened only for collection close after use.
- Unreachable hosts and native fact failures are omitted from the returned
  mapping. Missing values are not filled from SQLite, and their stored
  observations remain unchanged. Successful `None` values are retained.
- `collect()` returns only values from the current collection. Store queries
  still include historical hosts.
- `FactCollector()` no longer takes a store. `gather()` queues requests, and
  `run()` returns request/result pairs in queue order without accessing storage.
  Callers can process the values or explicitly persist them.
- Ruff no longer runs from inside `pytest`. Importing `pyinfra_cli` in the
  integration tests patches subprocess handling through gevent, which conflicts
  with `pytest-ruff`. Dedicated CI and pre-commit checks still enforce Ruff.
- `FactStore.latest_for()` accepts a fact class as well as a canonical fact id.
- `FactStore.latest()`, `latest_for()`, and `history()` take normal positional
  and keyword fact arguments instead of an `args` dict.
- `latest_for()` selects one value per node for the given fact arguments.

### Fixed

- Collection works with `PARALLEL` below the inventory host count, including 1,
  without waiting for every host's deploy code to reach a barrier.
- Collection binds host and state context for each fact greenlet and predicate.
- Unexpected collection errors and cancellation reach every waiting caller
  with their traceback instead of releasing callers to read stale values.
- Collection connections do not activate hosts for deployment, and native fact
  failures do not change the deployment's active or failed host sets.
- Concurrent requests to one host serialise connector access and cleanup.
- Storage errors do not prevent collection or invalidate its cached results.
  Persistence errors occur only when callers explicitly write to the store.

### Removed

- Module-level `gather()`, the zero-argument `collect()` deploy, the per-host
  collection barrier, and `PrepareEndFlush`. Collection no longer depends on
  an unreleased `prepare_end` callback.

## [0.1.0] — 2026-11-04

Initial release.
