# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `gather()` replaces `register()` and now arms a `PrepareEndFlush` callback on
  the current run, so pyinfra collects gathered facts automatically at the end
  of the prepare phase. Nothing new to call in a deploy.

### Fixed

- `collect()` now waits for every active host before flushing, instead of
  letting the first host flush while the others read the store. A deploy file
  that gathers, collects, and then reads the store in the same file no longer
  races the read on a fresh store.
- `collect()` raises a `PyinfraError` when pyinfra's `PARALLEL` is smaller than
  the number of active hosts. The collection barrier needs every host to load at
  once, which such a run cannot do, so it would previously hang and then abort
  with an opaque `gevent` `LoopExit`.

### Changed

- Ruff no longer runs from inside `pytest`. `collect()` imports `pyinfra_cli`,
  which calls `gevent.monkey.patch_all()`; that leaves the stdlib `Popen` that
  `pytest-ruff` captured unable to fork, so every ruff check item raised
  `TypeError: 'NoneType' object is not callable`. Ruff is still enforced by the
  dedicated CI step and the pre-commit hooks.
- `register()` is renamed to `gather()`; `FactCollector.collect()` is renamed to
  `FactCollector.gather()` to match.
- The `collect()` deploy is now a fallback for a pyinfra that does not fire the
  `prepare_end` callback. It is harmless to leave in a deploy.
- `FactStore.latest_for()` accepts a fact class as well as a canonical fact id.
- `FactStore.latest()`, `latest_for()`, and `history()` take fact arguments as
  positional and keyword arguments, matching the `gather()` call, instead of an
  `args` dict.
- `latest_for()` selects one value per node for the given fact arguments.

## [0.1.0] — 2026-11-04

Initial release.
