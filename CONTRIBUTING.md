# Contributing to Bitscrape

Thanks for considering contributing. This document covers the process;
see [`docs/developer-guide/`](docs/developer-guide/index.md) for the
technical mechanics (test suite, linting, project layout).

## Before you start

- **For a bug fix**: open an issue describing the bug with a minimal
  reproduction if possible, or go straight to a PR if it's small and
  obvious.
- **For a new feature**: open an issue or discussion first describing the
  concrete problem or use case, not just a technology/pattern name. This
  project has a track record of evaluating and declining feature requests
  that don't map to a demonstrated gap (see `ROADMAP.md`'s "explicit
  non-goals") -- a clear problem statement gets a faster, more useful
  response than "add X."
- **For anything security-sensitive**: see [`SECURITY.md`](SECURITY.md)
  instead of a public issue.

## Development setup

```bash
git clone <this repo>
cd bitscrape-fixes
python3 -m venv venv
source venv/bin/activate
pip install -e ".[all,dev,cli]"
```

Some tests need a local Redis on port 6390:
```bash
redis-server --daemonize no --port 6390 --bind 127.0.0.1 &
```

## Before opening a PR

```bash
pytest -q                  # should show all tests passing
ruff check src/             # should print "All checks passed!"
mypy src/bitscrape/<your files>.py --ignore-missing-imports
```

See [`docs/developer-guide/`](docs/developer-guide/index.md) for the known,
accepted pre-existing mypy false-positive on bare `Settings()` calls -- not
something you need to fix as part of an unrelated PR.

## What a good PR looks like

1. **A real test for the behavior you're adding or fixing.** This
   project's standard throughout its history: prefer testing against a
   real local instance of whatever the code talks to (a real HTTP server,
   a real Redis, a real file on disk, a real S3 API emulation via `moto`)
   over mocking, whenever that's feasible in a test environment. If you
   truly can't test something for real (it needs a cloud service, a
   specific OS, a live third-party API), say so explicitly in the
   docstring and test file rather than mocking around the gap silently.
2. **An updated `CHANGELOG.md` entry** describing what changed, what's
   tested and how, and anything explicitly not implemented or verified.
3. **Updated `docs/`** if the change affects user-facing behavior -- find
   the relevant page under `docs/` (there's very likely one already) and
   update it rather than leaving docs to drift from behavior.
4. **No unrelated changes.** Keep PRs focused; a drive-by refactor bundled
   with a bug fix makes both harder to review.

## Code style

- Follow the existing style in the file you're editing; `ruff` will catch
  most formatting/lint issues (`ruff check --fix src/` for auto-fixable
  ones).
- Prefer small, focused modules over growing existing files indefinitely --
  look at how `canonicalize.py`, `entity_resolution.py`, etc. are each
  scoped to one concern.
- Docstrings should be honest about test coverage and scope -- see any
  existing module (`storage/backends.py` is a good example) for the tone:
  state plainly what's verified, what's implemented-but-unverified, and
  what's an intentional stub.

## Review process

A PR needs:
- Passing tests (CI or your own local run, documented in the PR description).
- No new `ruff`/`mypy` findings on the files you touched.
- A clear description of what changed and why, including how you tested it.

Maintainers may ask for changes or additional tests before merging --
this isn't a rejection, just part of keeping the "every feature has a real
test" standard consistent across contributors.

## License

By contributing, you agree your contributions are licensed under this
project's [MIT License](LICENSE).
