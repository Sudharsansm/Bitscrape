# Installation

## Requirements

- Python **3.11** or higher
- pip or uv

---

## Install with pip

```bash
pip install bitscrape
```

## Install with uv (faster)

```bash
uv add bitscrape
```

---

## Optional Extras

Bitscrape has optional feature groups you can install as needed.

### JavaScript Rendering (Playwright)

For scraping sites that require JavaScript to load content:

```bash
pip install "bitscrape[playwright]"
playwright install chromium
```

### Distributed Crawling (Redis)

For running multiple workers sharing one queue:

```bash
pip install "bitscrape[redis]"
```

### PostgreSQL Storage

For saving scraped data directly to PostgreSQL or Supabase:

```bash
pip install "bitscrape[postgres]"
```

### LangGraph Workflow

For the state machine crawl orchestration engine:

```bash
pip install "bitscrape[workflow]"
```

### Faster Event Loop (Linux / macOS only)

```bash
pip install "bitscrape[speed]"
```

### Install Everything

```bash
pip install "bitscrape[full]"
```

### Development Install

```bash
pip install "bitscrape[dev]"
```

Includes: pytest, pytest-asyncio, pytest-cov, mypy, ruff, aioresponses, build, twine.

---

## Install from GitHub (latest dev version)

```bash
pip install git+https://github.com/yourorg/bitscrape.git
```

## Install from Source

```bash
git clone https://github.com/yourorg/bitscrape.git
cd bitscrape
pip install -e ".[dev]"
```

The `-e` flag installs in editable mode — changes to the source are reflected
immediately without reinstalling.

---

## Verify Installation

```bash
python -c "import bitscrape; print(bitscrape.__version__)"
bitscrape --version
```

---

## Upgrading

```bash
pip install --upgrade bitscrape
# or
uv add bitscrape --upgrade
```

---

## Virtual Environments (Recommended)

Always use a virtual environment to avoid dependency conflicts:

```bash
# Create
python -m venv .venv

# Activate (Windows)
.venv\Scripts\activate

# Activate (macOS / Linux)
source .venv/bin/activate

# Install
pip install bitscrape
```

Or with uv (handles venv automatically):

```bash
uv init myproject
cd myproject
uv add bitscrape
```
