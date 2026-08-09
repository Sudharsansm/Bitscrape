# CLI Reference

The `bitscrape` command-line tool (installed via the `cli` extra:
`pip install -e ".[cli]"`).

```bash
bitscrape --help
bitscrape --version
```

`--version` reads the real installed package version dynamically (fixed
from a previously hardcoded stale string -- see `CHANGELOG.md` 0.2.1).

## `bitscrape crawl`

Run a spider.

```bash
bitscrape crawl SPIDER_PATH [OPTIONS]
```

`SPIDER_PATH` is a path to a `.py` file containing a `Spider` subclass
(the CLI imports the file and instantiates the first `Spider` subclass it
finds).

| Option | Default | Meaning |
|---|---|---|
| `-o, --output PATH` | none (prints to stdout summary only) | Output file path, e.g. `data.jsonl` |
| `--fmt [jsonl\|json\|csv\|xml]` | `jsonl` | Output format |
| `--log-level TEXT` | `INFO` | Python logging level |
| `--no-robots` | off | Ignore robots.txt for this run (sets `robotstxt_obey=False`) |
| `--concurrency INT` | uses `Settings` default | Override `concurrent_requests` |

```bash
bitscrape crawl spiders/quotes.py -o quotes.jsonl
bitscrape crawl spiders/quotes.py -o quotes.csv --fmt csv
bitscrape crawl spiders/quotes.py --no-robots --concurrency 32 --log-level DEBUG
```

Internally, `crawl` builds a `Settings` object from these flags and calls
`build_engine()` -- the same factory function `bitscrape.run()` uses (see
[architecture/](../architecture/index.md)) -- so there is exactly one code
path deciding how configuration becomes a running crawl.

On completion, prints a summary table:
```
       Crawl Stats
+------------------------+--------+
| Requests               | 12     |
| Failed                 | 0      |
| Items scraped          | 45     |
| Items dropped          | 0      |
| Items noindexed        | 0      |
| Links nofollow-skipped | 0      |
| Downloaded             | 128.4 kB |
| Elapsed                | 3.21s  |
| RPS                    | 3.7    |
+------------------------+--------+
```

## `bitscrape list`

List spider files in a directory.

```bash
bitscrape list [--dir spiders]
```

- Scans the given directory (default `spiders/`, relative to your current
  working directory) for `.py` files and prints their filenames
  (alphabetically, excluding `__init__.py`).
- **Known limitation**: this lists filenames, not each file's actual
  `Spider.name` attribute or class name -- if `quotes.py` contains a class
  with `name = "my_custom_name"`, `list` still shows `quotes`. It also
  doesn't import/validate the files, so a file with no valid `Spider`
  subclass (or a syntax error) still appears in the list.

```bash
$ bitscrape list --dir spiders
  * news
  * quotes
```

## `bitscrape startproject`

Scaffold a new project directory.

```bash
bitscrape startproject NAME
```

Creates:
```
NAME/
  spiders/__init__.py
  pipelines/__init__.py
  items/__init__.py
  settings.py          # from bitscrape.core.settings import Settings; settings = Settings()
  scrapy.cfg            # [settings] default = NAME.settings
  README.md
```

Fails if `NAME/` already exists (exits non-zero rather than overwriting).

## `bitscrape genspider`

Generate a spider template file.

```bash
bitscrape genspider NAME DOMAIN [--template basic|crawl|sitemap]
```

Writes `spiders/NAME.py` (creates the `spiders/` directory if needed).
Three templates:

- **`basic`** (default) -- a single `parse()` callback hitting `https://DOMAIN`.
- **`crawl`** -- a `parse()` that both yields items and follows links via `self.follow()`.
- **`sitemap`** -- a two-callback spider: `parse()` reads `/sitemap.xml`,
  `parse_page()` handles each URL found in it.

```bash
bitscrape genspider quotes example.com --template crawl
```

## Notes

- All commands are defined in `bitscrape.cli.main` using `click` +
  `rich` for output formatting.
- The CLI and `bitscrape.run()` are intentionally kept in sync by both
  calling `build_engine()` -- see `CHANGELOG.md` 0.6.0/0.7.0 for the history
  of a real bug (missing `MetaRobotsMiddleware` in one of the two paths)
  this consolidation fixed.

## See also

- [quickstart/](../quickstart/index.md) -- the CLI in a minimal end-to-end example.
- [user-guide/](../user-guide/index.md) -- the full `Settings` reference these flags map onto.
