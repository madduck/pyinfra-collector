# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `register()` now arms a `PrepareEndFlush` callback on the current run, so
  pyinfra collects registered facts automatically at the end of the prepare
  phase. Nothing new to call in a deploy.

### Changed

- The `collect()` deploy is now a fallback for a pyinfra that does not fire the
  `prepare_end` callback. It is harmless to leave in a deploy.
- `FactStore.latest_for()` accepts a fact class as well as a canonical fact id.
- `FactStore.latest()`, `latest_for()`, and `history()` take fact arguments as
  positional and keyword arguments, matching the `register()` call, instead of
  an `args` dict.
- `latest_for()` selects one value per node for the given fact arguments.

## [0.1.0] — 2026-11-04

Initial release.
