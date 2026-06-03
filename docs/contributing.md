# Contributing to Bitscrape

Thank you for your interest in contributing! This guide explains how to
set up a development environment, run tests, and submit changes.

---

## Development Setup

```bash
# Clone the repository
git clone https://github.com/yourorg/bitscrape.git
cd bitscrape

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate      # macOS / Linux
.venv\Scripts\activate         # Windows

# Install in editable mode with dev dependencies
pip install -e ".[dev]"
pip install selectolax          # fast CSS parser
```

---

## Project Structure

```
bitscrape/
├── bitscrape/
│   ├── __init__.py         ← public API (import bitscrape)
│   ├── engine.py           ← central crawl loop
│   ├── core/
│   │   ├── models.py       ← Request, Response, BaseItem, CrawlStats
│   │   ├── settings.py     ← Settings (pydantic-settings)
│   │   └── spider.py       ← Spider base class
│   ├── scheduler/
│   │   ├── dupefilter.py   ← URL fingerprinting + dedup
│   │   └── scheduler.py    ← MemoryQueue + RedisQueue
│   ├── downloader/
│   │   └── downloader.py   ← aiohttp + Playwright
│   ├── parser/
│   │   └── selector.py     ← CSS/XPath + ParsedResponse
│   ├── middleware/
│   │   └── middleware.py   ← UserAgent, Robots, Cookies
│   ├── pipeline/
│   │   └── pipelines.py    ← Logging, Validation, Dedup, Postgres
│   ├── exporters/
│   │   └── feed.py         ← JSONL, JSON, CSV, XML
│   ├── workflow/
│   │   └── graph.py        ← LangGraph state machine
│   └── cli/
│       └── main.py         ← Click CLI
├── tests/
│   └── unit/
│       └── test_core.py    ← unit tests
├── examples/
│   ├── quotes_spider.py
│   └── ecommerce_spider.py
├── docs/                   ← documentation
└── pyproject.toml
```

---

## Running Tests

```bash
# Run all tests
pytest tests/ -v

# Run with coverage report
pytest tests/ --cov=bitscrape --cov-report=term-missing

# Run a specific test file
pytest tests/unit/test_core.py -v

# Run a specific test
pytest tests/unit/test_core.py::test_scheduler_dedup -v
```

---

## Linting and Formatting

```bash
# Check for linting issues
ruff check bitscrape/

# Auto-fix linting issues
ruff check bitscrape/ --fix

# Check formatting
ruff format --check bitscrape/

# Auto-format
ruff format bitscrape/
```

---

## Type Checking

```bash
mypy bitscrape/
```

---

## Adding a New Feature

1. **Create a branch:**
   ```bash
   git checkout -b feature/my-feature
   ```

2. **Write the code** in the appropriate module

3. **Write tests** in `tests/unit/` or `tests/integration/`

4. **Update documentation** in `docs/`

5. **Run checks:**
   ```bash
   pytest tests/ -v
   ruff check bitscrape/
   mypy bitscrape/
   ```

6. **Commit and push:**
   ```bash
   git add .
   git commit -m "feat: add my feature"
   git push origin feature/my-feature
   ```

7. **Open a Pull Request** on GitHub

---

## Commit Message Format

Use conventional commits:

```
feat: add proxy rotation middleware
fix: handle empty response body in parser
docs: update selectors reference
test: add scheduler depth limit test
refactor: simplify downloader retry logic
chore: bump pydantic to 2.7
```

---

## Reporting Bugs

Open an issue at: https://github.com/yourorg/bitscrape/issues

Include:
- Bitscrape version (`bitscrape --version`)
- Python version (`python --version`)
- Operating system
- Minimal code to reproduce the bug
- Full error traceback

---

## Code Style

- **Python 3.11+** — use modern type hints (`list[str]` not `List[str]`)
- **async/await** everywhere — no blocking I/O in the hot path
- **Pydantic models** for all data contracts
- **Type annotations** on all public functions and methods
- **Docstrings** on all public classes and methods
- **f-strings** for string formatting
- Line length: 100 characters (enforced by ruff)
