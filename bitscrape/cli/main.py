"""
Bitscrape CLI
=============
Commands:
  bitscrape crawl <spider>  [-o output] [--fmt jsonl|json|csv|xml]
  bitscrape startproject <name>
  bitscrape genspider <name> <domain>
  bitscrape list
  bitscrape version
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table

console = Console()


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


# ---------------------------------------------------------------------------
# Main group
# ---------------------------------------------------------------------------


@click.group()
@click.version_option("0.1.0", prog_name="bitscrape")
def cli() -> None:
    """⚡ Bitscrape — modern async web scraping framework."""


# ---------------------------------------------------------------------------
# crawl
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("spider_path")
@click.option("-o", "--output", default=None, help="Output file path (e.g. data.jsonl)")
@click.option(
    "--fmt",
    default="jsonl",
    type=click.Choice(["jsonl", "json", "csv", "xml"], case_sensitive=False),
    show_default=True,
    help="Output format",
)
@click.option("--log-level", default="INFO", show_default=True)
@click.option("--no-robots", is_flag=True, help="Ignore robots.txt")
@click.option("--concurrency", default=None, type=int, help="Max concurrent requests")
def crawl(
    spider_path: str,
    output: str | None,
    fmt: str,
    log_level: str,
    no_robots: bool,
    concurrency: int | None,
) -> None:
    """
    Run a spider.

    SPIDER_PATH is either:
      - A Python module path like  myproject.spiders.quotes
      - A .py file path like        spiders/quotes.py

    Examples:

      bitscrape crawl myproject.spiders.quotes -o out.jsonl

      bitscrape crawl spiders/quotes.py --fmt csv
    """
    _setup_logging(log_level)

    spider_cls = _load_spider(spider_path)
    if spider_cls is None:
        console.print(f"[red]Could not find a Spider subclass in {spider_path!r}[/red]")
        sys.exit(1)

    from bitscrape.core.settings import Settings
    from bitscrape.engine import Engine
    from bitscrape.exporters.feed import get_exporter
    from bitscrape.middleware.middleware import (
        CookieMiddleware,
        RobotsMiddleware,
        UserAgentMiddleware,
    )

    overrides: dict[str, Any] = {}
    if no_robots:
        overrides["robotstxt_obey"] = False
    if concurrency:
        overrides["concurrent_requests"] = concurrency

    settings = Settings(**overrides)
    spider = spider_cls(settings=settings)

    exporter = get_exporter(fmt, output) if output else get_exporter(fmt, None)

    middlewares = [UserAgentMiddleware(), CookieMiddleware()]
    if settings.robotstxt_obey:
        middlewares.insert(0, RobotsMiddleware())

    engine = Engine(
        spider=spider,
        settings=settings,
        middlewares=middlewares,
        exporter=exporter,
    )

    console.print(
        Panel(
            f"[bold cyan]Running spider:[/bold cyan] [yellow]{spider.name}[/yellow]",
            expand=False,
        )
    )

    try:
        stats = asyncio.run(engine.run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user[/yellow]")
        sys.exit(0)

    # Summary table
    t = Table(title="Crawl Stats", show_header=False)
    t.add_column("Key", style="cyan")
    t.add_column("Value", style="white")
    t.add_row("Requests", str(stats.requests_made))
    t.add_row("Failed", str(stats.requests_failed))
    t.add_row("Items scraped", str(stats.items_scraped))
    t.add_row("Items dropped", str(stats.items_dropped))
    t.add_row("Downloaded", f"{stats.bytes_downloaded / 1024:.1f} kB")
    t.add_row("Elapsed", f"{stats.elapsed:.2f}s")
    t.add_row("RPS", f"{stats.rps:.1f}")
    console.print(t)


# ---------------------------------------------------------------------------
# startproject
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("name")
def startproject(name: str) -> None:
    """Create a new Bitscrape project scaffold."""
    root = Path(name)
    if root.exists():
        console.print(f"[red]Directory {name!r} already exists[/red]")
        sys.exit(1)

    (root / "spiders").mkdir(parents=True)
    (root / "pipelines").mkdir()
    (root / "items").mkdir()

    (root / "settings.py").write_text(
        "from bitscrape.core.settings import Settings\n\nsettings = Settings()\n"
    )
    (root / "spiders" / "__init__.py").touch()
    (root / "pipelines" / "__init__.py").touch()
    (root / "items" / "__init__.py").touch()
    (root / "scrapy.cfg").write_text(f"[settings]\ndefault = {name}.settings\n")
    (root / "README.md").write_text(f"# {name}\n\nA Bitscrape project.\n")

    console.print(f"[green]Created project[/green] [bold]{name}/[/bold]")
    console.print(f"  cd {name} && bitscrape genspider myspider example.com")


# ---------------------------------------------------------------------------
# genspider
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("name")
@click.argument("domain")
@click.option("--template", default="basic", type=click.Choice(["basic", "crawl", "sitemap"]))
def genspider(name: str, domain: str, template: str) -> None:
    """Generate a spider template."""
    dest = Path("spiders") / f"{name}.py"
    dest.parent.mkdir(exist_ok=True)

    if template == "basic":
        code = _BASIC_TEMPLATE.format(name=name, domain=domain)
    elif template == "crawl":
        code = _CRAWL_TEMPLATE.format(name=name, domain=domain)
    else:
        code = _SITEMAP_TEMPLATE.format(name=name, domain=domain)

    dest.write_text(code)
    console.print(f"[green]Created[/green] {dest}")


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@cli.command(name="list")
@click.option("--dir", "spiders_dir", default="spiders", show_default=True)
def list_spiders(spiders_dir: str) -> None:
    """List all spiders in the project."""
    p = Path(spiders_dir)
    if not p.exists():
        console.print(f"[red]No spiders directory found at {spiders_dir!r}[/red]")
        return
    names = [f.stem for f in p.glob("*.py") if f.stem != "__init__"]
    if not names:
        console.print("[yellow]No spiders found[/yellow]")
    for n in sorted(names):
        console.print(f"  • {n}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_spider(path_or_module: str) -> type | None:
    """Load a spider class from a file path or dotted module path."""
    import inspect

    from bitscrape.core.spider import Spider

    # File path
    if path_or_module.endswith(".py"):
        spec = importlib.util.spec_from_file_location("_bitscrape_spider", path_or_module)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    else:
        mod = importlib.import_module(path_or_module)

    for _, obj in inspect.getmembers(mod, inspect.isclass):
        if issubclass(obj, Spider) and obj is not Spider and obj.name:
            return obj
    return None


# ---------------------------------------------------------------------------
# Spider templates
# ---------------------------------------------------------------------------

_BASIC_TEMPLATE = """\
from bitscrape.core.spider import Spider
from bitscrape.parser.selector import ParsedResponse


class {name}Spider(Spider):
    name = "{name}"
    start_urls = ["https://{domain}/"]

    async def parse(self, response: ParsedResponse):
        # TODO: implement parsing
        title = response.css("title::text").get()
        yield {{"url": response.url, "title": title}}
"""

_CRAWL_TEMPLATE = """\
from bitscrape.core.spider import Spider
from bitscrape.core.models import Request
from bitscrape.parser.selector import ParsedResponse


class {name}Spider(Spider):
    name = "{name}"
    start_urls = ["https://{domain}/"]

    async def parse(self, response: ParsedResponse):
        # Extract items
        for item in response.css("article"):
            yield {{"title": item.css("h2::text").get(),
                   "link": item.css("a::attr(href)").get()}}

        # Follow pagination
        nxt = response.css("a.next::attr(href)").get()
        if nxt:
            yield self.follow(nxt)
"""

_SITEMAP_TEMPLATE = """\
from bitscrape.core.spider import Spider
from bitscrape.parser.selector import ParsedResponse


class {name}Spider(Spider):
    name = "{name}"
    start_urls = ["https://{domain}/sitemap.xml"]

    async def parse(self, response: ParsedResponse):
        for url in response.css("loc::text").getall():
            yield self.follow(url, callback="parse_page")

    async def parse_page(self, response: ParsedResponse):
        yield {{"url": response.url,
               "title": response.css("title::text").get()}}
"""
