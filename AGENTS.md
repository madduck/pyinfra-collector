# `pyinfra-collector`

Guidance for coding agents working in this repository. Read this together with
the user-level `AGENTS.md`, which takes precedence where the two overlap.

## Product

Collect [pyinfra](https://pyinfra.com) facts across the whole inventory into a
local SQLite store, so deploys can read values from every host on the controller
side.

## Stack

- Python ≥3.14
- `uv` for dependency management, no `pip` / `requirements.txt`.

## Project Structure

```raw
./
├── src/pyinfra_collector/  # package
│   ├── collector.py        # fact collection across the inventory
│   └── store.py            # SQLite persistence for collected facts
├── tests/                  # pytest test suite
├── pyproject.toml          # project metadata & tool config
├── .editorconfig           # common editor configuration
├── .gitignore              # Git ignore file
└── .pre-commit-config.yaml # `pre-commit` configuration
```

## Setup & Build

- `uv add <dependency>` to add a dependency (never `uv pip`).
- `uv add --dev <dependency>` to add a development dependency.
- `uv sync` to create the environment and install the package.
- `uv build` for production wheels.
- `uv run <tool>` to run any tool (`pytest`, `mypy`, `ruff`, `bandit`).

## Quality Gates

| Gate     | Tool                            |
| -------- | ------------------------------- |
| Format   | `ruff format --diff`            |
| Lint     | `ruff check --show-fixes`       |
| Types    | `mypy .`                        |
| Security | `bandit -c pyproject.toml -r .` |
| Tests    | `pytest`                        |

All gates run via pre-commit hooks; only `pytest` needs a manual run (with
`uv run`).

The pre-commit config lives in `.pre-commit-config.yaml`.

A task is done only when docstrings, other comments, README, and manpage are
updated, and `uv run pytest` passes with 100% coverage.

### Mypy configuration

Set `warn_unreachable = true`.

Set `ignore_missing_imports = true` for module `ipdb`.

The Mypy pre-commit hook runs `uv run mypy .`, so it sees the project
environment and its dependencies; there is no separate dependency list to
maintain.

### Coverage

```toml
exclude_also = [
  'def __repr__',
  'if TYPE_CHECKING:',
  'class .*\bProtocol\):',
  '@overload',
  '@(abc\.)?abstractmethod',
  'raise NotImplementedError',
  'raise AssertionError',
]
```

### Bandit

Skip `assert_used` for test files.

## Git Workflow

Each task goes on a **separate feature branch** created from `main`:

```sh
git checkout main
git checkout -b feature/<short-name>
```

Never commit to `main`.

Always bundle commits which update the tests, change the code, and reflect the
new behaviour in all documentation, docstrings, and comments. Do not split a
change's tests, code, or documentation across separate commits.

Scope-prefixed messages (e.g. `docs:`, `ci:`, `collector:`) commit messages,
not Conventional Commits.

While a feature branch has not been merged into `main`, related follow-up work
(bugfixes, tweaks) should be appended to the appropriate feature branch. If it
is not clear to you what branch to use, let me choose from a list of
possibilities.

## Coding Conventions

- Google-style docstrings on every public function.
- Strict formatting according to Ruff;
  - Prefer test D211 over D203.
  - Prefer test D213 over D212.
- Type hints everywhere (use `| None` / `list[X]` / `dict[K, V]`).
- Relative imports within the package (`from .foo import bar`).
- Module-level imports preferred; inline imports only for tests, otherwise with
  approval; Select Ruff test "PLC0415" except for tests.
- EAFP: catch exceptions instead of pre-checking.
- `pathlib` over `os.path`; f-strings over `%` / `.format()`.
- Use comprehensions, generators, decorators where they fit.

## Test Conventions

- 100% line + branch coverage goal. `# pragma: no cover` only on defensive
  branches that cannot be triggered (document why).
- Red / green: write the test, make it pass, keep it green.
- Prefer behaviour-based assertions over introspection of private attributes.
- Deterministic fixtures (factories / `tmp_path` / `CliRunner`).
- `pytest.mark.xfail` instead of commenting out a failing test.
- Run affected tests before the full suite.

## Text encoding

- Write typographic characters directly as UTF-8 (for example ’ “ ” – — é © …),
  not as HTML/XML entities or numeric references such as `&#8217;`, `&#x2019;`,
  or `&rsquo;`.
- Use escaping only where the target syntax requires it, such as `&`, `<`, and
  `>` inside HTML or XML markup, or where a format explicitly needs ASCII.
  - Save all text files as UTF-8.

## Ignore

Treat everything in `.gitignore` as off-limits to read or edit. On top of that,
never open:

- Secrets and `.env` files
- Large data files unrelated to the current task
- Vendored or generated code
