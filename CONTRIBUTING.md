# Contributing to Bitscrape

Thank you for your interest in contributing! Here's everything you need.

## Setting Up

```bash
git clone https://github.com/YOUR_USERNAME/bitscrape.git
cd bitscrape
pip install -e ".[dev]"
pip install selectolax
```

## Running Tests

```bash
pytest tests/ -v
```

## Code Style

```bash
ruff check bitscrape/
ruff format bitscrape/
```

## Submitting a Pull Request

1. Fork the repo and create a branch: `git checkout -b feat/my-feature`
2. Make your changes and add tests
3. Ensure all tests pass: `pytest tests/`
4. Push and open a Pull Request against `main`

## Reporting Bugs

Open an issue at https://github.com/YOUR_USERNAME/bitscrape/issues with:
- Python version
- Bitscrape version (`pip show bitscrape`)
- Minimal reproduction script
- Full traceback
