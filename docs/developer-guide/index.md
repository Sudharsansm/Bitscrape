# Developer Guide

For people working on Bitscrape itself, not just using it. See also the
root [`CONTRIBUTING.md`](../../CONTRIBUTING.md) for the process
(branching, PRs, review expectations) — this page is about the mechanics
of the codebase.

## Setting up a dev environment

```bash
git clone <this repo>
cd bitscrape-fixes
python3 -m venv venv
source venv/bin/activate
pip install -e ".[all,dev,cli]"
```

Some tests need a real local Redis on port 6390 (not the default 6379, to
avoid colliding with a Redis you might already be running) and a real
local PostgreSQL server:

```bash
redis-server --daemonize no --port 6390 --bind 127.0.0.1 &
sudo service postgresql start
sudo -u postgres psql -c "ALTER USER postgres PASSWORD 'postgres';"
sudo -u postgres createdb bitscrape_test
```

## Running the test suite

```bash
pytest -q
```

Expect `440 passed`. If Redis isn't running, only the handful of tests
that specifically need it will fail — everything else still passes. Run a
single file or test:

```bash
pytest tests/test_engine_diagnostics.py -q
pytest tests/test_frontier.py::test_politeness_enforced_within_a_single_domain -v
```

## Linting and type-checking

```bash
ruff check src/           # should print "All checks passed!"
mypy src/bitscrape/<file>.py --ignore-missing-imports
```

`ruff` and `mypy` versions are pinned exactly in `pyproject.toml`'s `dev`
extra (`ruff==0.15.22`, `mypy==2.3.0`) — this is deliberate. An earlier
release discovered that letting `ruff` float to `>=0.4` meant a fresh
install could resolve a newer version with stricter default rules, which
then flagged pre-existing, untouched code that the originally-verified
version never complained about. If you need to bump these pins, re-run
`ruff check src/` against the *whole* tree afterward and fix or
consciously `noqa` anything new before merging the bump.

### Known, accepted mypy gap

`mypy` reports false-positive errors on every bare `Settings()` call
(e.g. in `engine.py`, `factory.py`, `__init__.py`) — a known
`pydantic-settings`/`mypy` plugin interaction, not a real bug. Confirmed at
runtime by literally every test in the suite that constructs a `Settings()`
successfully. Don't spend time trying to "fix" this without configuring
the pydantic mypy plugin project-wide, which is a bigger, separate change.

## Testing philosophy

Every feature added to this project follows the same standard: **if it's
documented as working, there's a real test proving it**, not a mock
standing in for the thing being tested. Concretely, this project's tests
use:

- A **real local Redis instance** for anything Redis-related (dedup filter,
  distributed throttle) — including tests with two independent client
  instances, to prove cross-process coordination actually works.
- A **real local HTTP server** (`aiohttp.test_utils.TestServer` for
  async-context tests, or a plain `threading` + `http.server` server for
  testing `bitscrape.run()`, which manages its own event loop — see the
  note below on why these two aren't interchangeable) for anything
  crawl-related.
- A **real on-disk SQLite database** for `SQLiteStorageBackend`.
- A **real S3 API emulation via `moto`** (not a hand-rolled mock) for
  `S3StorageBackend`.
- **Real OpenTelemetry SDK spans** (via OTel's own in-memory exporter) and
  **real `prometheus_client` metrics** (scraped via a real HTTP client) for
  the observability module.

Where a real backend genuinely wasn't available in the environment this
project was built in (a live PostgreSQL/MongoDB/Elasticsearch server, a
Kubernetes cluster, a Docker daemon, an embedding-model API), that's
disclosed explicitly in the code's docstrings and in these docs — either
as "implemented, mock-tested, needs your own integration test" or as an
honest `NotImplementedError` stub with the intended implementation shown
in the docstring, rather than claimed as verified when it wasn't.

### A real gotcha worth knowing: `aiohttp.test_utils.TestServer` is event-loop-bound

`TestServer` is tied to whichever asyncio event loop started it. Once that
loop closes, the server silently stops processing requests even though its
socket stays open — this caused a real hang when testing
`bitscrape.run()` (which creates its **own**, separate internal event loop
via `asyncio.run()`) against a `TestServer` started in an outer loop. If
you're testing something that itself calls `asyncio.run()`, use a
plain `threading.Thread` + `http.server.HTTPServer` instead (see
`tests/test_package_api.py`'s `_ThreadedTestServer` for the pattern) — it
has no event-loop coupling.

## Project layout

See [getting-started/index.md](../getting-started/index.md#project-layout-at-a-glance)
for the directory tree.

## Adding a new feature

1. Check whether it fits an existing module or needs a new one — this
   project favors small, focused modules (`canonicalize.py`,
   `entity_resolution.py`, etc.) over growing existing files indefinitely.
2. If it needs a new `Settings` field, add it with a sensible default that
   preserves existing behavior when unset (most new features in this
   project default to `False`/off).
3. Write the implementation with real tests as you go — not after. Prefer
   testing against a real local instance of whatever the feature talks to
   (a real server, a real Redis, a real file on disk) over mocking,
   whenever that's feasible in a test environment.
4. If the feature can't be verified for real in your environment (needs a
   cloud service, a specific OS, etc.), say so explicitly in the
   docstring/tests rather than mocking around the gap silently.
5. Run `pytest -q`, `ruff check src/`, and `mypy <your files> --ignore-missing-imports`
   before considering it done.
6. Update `CHANGELOG.md` and the relevant `docs/` page.

## Release process (for maintainers)

1. Bump `version` in `pyproject.toml`.
2. Add a dated entry at the top of `CHANGELOG.md` describing what changed,
   what was tested and how, and anything explicitly not implemented.
3. Reinstall (`pip install -e ".[all,dev,cli]"`) and re-run the full test
   suite + lint + `bitscrape --version` / `python -c "import bitscrape; print(bitscrape.__version__)"`
   to confirm the version bump actually propagated everywhere.
4. Regenerate `bitscrape_fixes.patch` if you're maintaining it as a
   standalone diff against upstream:
   ```bash
   diff -ru path/to/upstream/bitscrape src/bitscrape > bitscrape_fixes.patch
   ```
5. Do a genuine clean-room check: copy the final directory somewhere else,
   create a fresh venv, install from scratch, and run the test suite there
   — not just in your working directory, which can accumulate stale state.

## Where to ask questions / report issues

See [`CONTRIBUTING.md`](../../CONTRIBUTING.md) and
[`SECURITY.md`](../../SECURITY.md) (for anything security-sensitive
specifically).
