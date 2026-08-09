# Installation

## Requirements

- Python 3.11 or 3.12
- pip (or another PEP 517-compatible installer)
- Optional, only if you enable the corresponding feature:
  - A running Redis server (`scheduler_use_redis`, `distributed_throttle_enabled`)
  - Playwright's browser binaries (`playwright_pool_enabled`, `use_playwright=True` on a request)
  - A PostgreSQL server (`PostgresStorageBackend`, `PostgresPipeline`)

## Install from the extracted source

```bash
cd bitscrape-fixes      # the folder containing pyproject.toml
python3 -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\Activate.ps1
pip install -e ".[all,dev,cli]"
```

Verify:

```bash
bitscrape --version
python -c "import bitscrape; print(bitscrape.__version__)"
```

Both should print the same version (`0.7.0` at the time of writing — both
paths read the real installed package metadata dynamically, not a
hardcoded string).

## Choosing what to install

`pyproject.toml` defines optional extras so you only pull in dependencies
for features you actually use:

| Extra | Adds | Needed for |
|---|---|---|
| `cli` | `rich`, `click` | The `bitscrape` command-line tool |
| `playwright` | `playwright` (Python package only) | JS-rendered pages via `use_playwright=True` |
| `redis` | `redis`, `orjson` | `scheduler_use_redis`, `distributed_throttle_enabled`, `RedisQueue`, `RedisDupeFilter` |
| `storage-sqlite` | `aiosqlite` | `SQLiteStorageBackend` |
| `storage-postgres` | `asyncpg` | `PostgresStorageBackend`, `PostgresPipeline` |
| `storage-s3` | `boto3` | `S3StorageBackend` |
| `monitoring` | `psutil` | `StatsMonitor`'s CPU/RAM reporting |
| `knowledge-graph` | `networkx`, `numpy`, `scipy` | `LinkGraph`, `KnowledgeGraph` |
| `observability` | `prometheus-client`, `opentelemetry-api`, `opentelemetry-sdk` | `CrawlMetrics`, `CrawlTracer` |
| `dev` | `pytest`, `pytest-asyncio`, `mypy`, `ruff`, `moto[s3]`, `pyyaml` | Running the test suite |
| `all` | everything above except `dev`/`cli` | Full feature set |

Minimal install (just the core crawler, no extras):

```bash
pip install -e .
```

Everything you need for local development and testing:

```bash
pip install -e ".[all,dev,cli]"
```

### Playwright's browser binaries

Installing the `playwright` extra only installs the Python driver. The
actual browser (Chromium/Firefox/WebKit) is a separate download:

```bash
playwright install chromium
```

Without this step, `use_playwright=True` will raise at request time, not
at import time.

## Setting up Redis (optional)

Only needed if you enable `scheduler_use_redis` or
`distributed_throttle_enabled`. Any of these work:

```bash
# Option A: a system package
sudo apt-get install redis-server && redis-server --daemonize yes

# Option B: Docker
docker run -d -p 6379:6379 redis:7-alpine

# Option C: the bundled compose file (also brings up a worker container)
docker compose -f deploy/docker-compose.yml up redis
```

Then point `Settings.redis_url` at it (default is
`redis://localhost:6379/0`).

## Setting up PostgreSQL (optional)

Only needed for `PostgresStorageBackend` or `PostgresPipeline`. Point
`Settings.database_url` (a standard `asyncpg` DSN) or the backend's `dsn`
argument at a running server:

```
postgresql://user:password@localhost:5432/bitscrape
```

> **Note**: `PostgresStorageBackend` is implemented against the real
> `asyncpg` API and unit-tested with a mock connection — it was not
> possible to verify it against a live PostgreSQL server in the environment
> this project was built in (the package mirror available didn't have
> `postgresql` packages). Run your own integration test against a real
> server before depending on it in production. See
> [storage/index.md#postgresql](../storage/index.md#postgresql).

## Troubleshooting installation

### `ERROR: ... does not appear to be a Python project`

`pip` needs to be run from the exact directory containing `pyproject.toml`
— not a parent directory, not a subdirectory. If you extracted a zip and
aren't sure where things landed:

```bash
# macOS/Linux
find . -name pyproject.toml

# Windows PowerShell
Get-ChildItem -Recurse -Filter pyproject.toml
```

`cd` into the folder the result is in, then retry.

### `bitscrape: command not found` right after install

Close and reopen your terminal (or re-activate the virtualenv) so the
newly-installed console script is picked up on `PATH`.

### Lint/type-check tool versions

The `dev` extra pins exact versions (`ruff==0.15.22`, `mypy==2.3.0`,
`pytest==9.1.1`, `pytest-asyncio==1.4.0`, `moto[s3]==5.2.2`,
`pyyaml==6.0.3`). This is deliberate — an earlier release discovered that
an unpinned `ruff` could resolve to a newer version with stricter default
rules, producing lint findings on untouched code that the tested version
never flagged. If your own fork changes these pins, re-verify
`ruff check src/` still passes cleanly on the whole tree.

## Next steps

→ [quickstart/](../quickstart/index.md)
