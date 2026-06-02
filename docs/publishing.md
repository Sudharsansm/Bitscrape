# Bitscrape — PyPI Publishing Guide

This guide covers everything needed to publish Bitscrape so anyone can install
it with:

```bash
pip install bitscrape
uv add bitscrape
```

---

## Step 1 — Create a PyPI Account

1. Go to https://pypi.org/account/register/
2. Verify your email address
3. Enable 2FA (required for publishing)

Also create a TestPyPI account for testing first:
https://test.pypi.org/account/register/

---

## Step 2 — Install Build Tools

```bash
pip install build twine

# or with uv:
uv add --dev build twine
```

---

## Step 3 — Build the Package

```bash
cd bitscrape/

# Build both a wheel (.whl) and source tarball (.tar.gz)
python -m build

# You should see:
# dist/
#   bitscrape-0.1.0-py3-none-any.whl
#   bitscrape-0.1.0.tar.gz
```

---

## Step 4 — Test on TestPyPI First (Recommended)

```bash
# Upload to TestPyPI
twine upload --repository testpypi dist/*

# Enter your TestPyPI credentials when prompted
# Username: your-username
# Password: your-password (or API token)

# Test installing from TestPyPI
pip install --index-url https://test.pypi.org/simple/ bitscrape

# Verify it works
python -c "import bitscrape; print(bitscrape.__version__)"
```

---

## Step 5 — Publish to Real PyPI

```bash
twine upload dist/*

# Enter your PyPI credentials when prompted
```

Done! Now anyone can install it:

```bash
pip install bitscrape
uv add bitscrape
```

---

## Step 6 — Using API Tokens (Recommended over password)

1. Go to https://pypi.org/manage/account/token/
2. Create a token scoped to the `bitscrape` project
3. Use it as the password (username = `__token__`):

```bash
twine upload dist/* -u __token__ -p pypi-AgEIcHlwaS5vcmcA...
```

Or store in `~/.pypirc`:

```ini
[pypi]
username = __token__
password = pypi-AgEIcHlwaS5vcmcA...your-full-token...

[testpypi]
repository = https://test.pypi.org/legacy/
username = __token__
password = pypi-test-...your-test-token...
```

---

## Step 7 — Automated Publishing via GitHub Actions (Best Practice)

This is the professional way — push a tag and PyPI publishes automatically.

### 7a. Push your code to GitHub

```bash
git init
git add .
git commit -m "feat: initial release v0.1.0"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/bitscrape.git
git push -u origin main
```

### 7b. Set up PyPI Trusted Publishing (no API token needed)

1. Log into https://pypi.org
2. Go to your project → **Manage** → **Publishing**
3. Add a new trusted publisher:
   - Owner:      `YOUR_GITHUB_USERNAME`
   - Repository: `bitscrape`
   - Workflow:   `publish.yml`
   - Environment: `pypi`

4. In GitHub → your repo → **Settings** → **Environments**
   → Create environment named `pypi`

### 7c. Tag a release to trigger publishing

```bash
# Bump version in pyproject.toml first, then:
git tag v0.1.0
git push origin v0.1.0

# GitHub Actions automatically:
# 1. Runs tests (ci.yml)
# 2. Builds the package (python -m build)
# 3. Publishes to PyPI (publish.yml)
```

---

## Updating the Version

Edit `pyproject.toml`:

```toml
[project]
version = "0.2.0"   # ← bump this
```

Also update `bitscrape/__init__.py`:

```python
__version__ = "0.2.0"
```

Then rebuild and publish:

```bash
python -m build
twine upload dist/*
# or just: git tag v0.2.0 && git push origin v0.2.0
```

---

## After Publishing — How Users Install It

```bash
# Basic install
pip install bitscrape

# With uv (faster)
uv add bitscrape

# With JavaScript rendering support
pip install "bitscrape[playwright]"
playwright install chromium

# With Redis distributed mode
pip install "bitscrape[redis]"

# With PostgreSQL storage
pip install "bitscrape[postgres]"

# Install everything
pip install "bitscrape[full]"

# Specific version
pip install "bitscrape==0.1.0"

# Latest dev version directly from GitHub
pip install git+https://github.com/YOUR_USERNAME/bitscrape.git
```

---

## Verify Your Published Package

```bash
pip install bitscrape
python -c "
import bitscrape
print('Version:', bitscrape.__version__)
print('Spider:', bitscrape.Spider)
print('Run:', bitscrape.run)
print('All good!')
"
```

---

## Package Page

After publishing, your package will be live at:
https://pypi.org/project/bitscrape/

Users can also find it by searching PyPI:
https://pypi.org/search/?q=bitscrape
