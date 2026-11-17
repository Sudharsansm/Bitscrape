"""
Bitscrape CLI
=============
Commands:
  bitscrape crawl <spider>  [-o output] [--fmt jsonl|json|csv|xml]
  bitscrape startproject <name>
  bitscrape genspider <name> <domain> [--template basic|crawl|sitemap|xmlfeed|csvfeed|shopify]
  bitscrape shell <url>  [--playwright]
  bitscrape get <url>  [--playwright] [--as markdown|html|text|structured] [--css SELECTOR]
  bitscrape mcp  [--transport stdio|sse|streamable-http]
  bitscrape list
  bitscrape version
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table

console = Console()


def _installed_version() -> str:
    """
    Resolve the installed package version dynamically instead of a hardcoded
    string, which previously went stale the moment the package was bumped
    (e.g. `--version` reported "0.1.0" even after installing 0.2.0).
    """
    try:
        return _pkg_version("bitscrape")
    except PackageNotFoundError:
        return "0.0.0+unknown"


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
@click.version_option(_installed_version(), prog_name="bitscrape")
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
    from bitscrape.exporters.feed import get_exporter
    from bitscrape.factory import build_engine

    overrides: dict[str, Any] = {}
    if no_robots:
        overrides["robotstxt_obey"] = False
    if concurrency:
        overrides["concurrent_requests"] = concurrency

    settings = Settings(**overrides)
    spider = spider_cls(settings=settings)

    exporter = get_exporter(fmt, output) if output else get_exporter(fmt, None)

    async def _build_and_run():
        engine = await build_engine(spider, settings, exporter=exporter)
        return engine, await engine.run()

    console.print(
        Panel(
            f"[bold cyan]Running spider:[/bold cyan] [yellow]{spider.name}[/yellow]",
            expand=False,
        )
    )

    try:
        engine, stats = asyncio.run(_build_and_run())
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
    t.add_row("Items noindexed", str(stats.items_noindexed))
    t.add_row("Links nofollow-skipped", str(stats.links_nofollow_skipped))
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
@click.option(
    "--template",
    default="basic",
    type=click.Choice(["basic", "crawl", "sitemap", "xmlfeed", "csvfeed", "shopify"]),
)
def genspider(name: str, domain: str, template: str) -> None:
    """Generate a spider template."""
    dest = Path("spiders") / f"{name}.py"
    dest.parent.mkdir(exist_ok=True)

    templates = {
        "basic": _BASIC_TEMPLATE,
        "crawl": _CRAWL_TEMPLATE,
        "sitemap": _SITEMAP_TEMPLATE,
        "xmlfeed": _XMLFEED_TEMPLATE,
        "csvfeed": _CSVFEED_TEMPLATE,
        "shopify": _SHOPIFY_TEMPLATE,
    }
    code = templates[template].format(name=name, domain=domain)

    dest.write_text(code)
    console.print(f"[green]Created[/green] {dest}")


@cli.command()
@click.argument("url")
@click.option("--playwright", is_flag=True, help="Render with a headless browser first.")
def shell(url: str, playwright: bool) -> None:
    """Fetch URL, then drop into an interactive shell with `response`/`r`
    (a ParsedResponse) already loaded, plus a `fetch(url)` helper to load
    another page in the same session without leaving the shell."""
    from bitscrape.core.models import Request
    from bitscrape.core.settings import Settings
    from bitscrape.curlparse import curl_to_request
    from bitscrape.downloader.downloader import Downloader
    from bitscrape.parser.selector import ParsedResponse

    settings = Settings()
    downloader = Downloader(settings)

    async def _fetch(target_url: str) -> ParsedResponse:
        await downloader.open()
        try:
            resp = await downloader.fetch(Request(url=target_url, use_playwright=playwright))
        finally:
            await downloader.close()
        return ParsedResponse(resp)

    def fetch(target_url: str) -> ParsedResponse:
        """Fetch another URL from within the shell; returns a ParsedResponse."""
        return asyncio.run(_fetch(target_url))

    def view(target: ParsedResponse | None = None) -> None:
        """Open a fetched response's HTML in your actual browser for visual
        inspection -- defaults to the most recently fetched `response`."""
        import tempfile
        import webbrowser

        target = target if target is not None else response
        with tempfile.NamedTemporaryFile("wb", suffix=".html", delete=False) as f:
            f.write(target.body)
            path = f.name
        webbrowser.open(f"file://{path}")
        console.print(f"[dim]Opened {path} in your browser[/dim]")

    def from_curl(curl_command: str) -> Request:
        """Paste a browser's "Copy as cURL" output; returns a Request you
        can inspect or pass to `fetch`-equivalent code -- e.g.
        `r2 = asyncio.run(_fetch(from_curl(cmd).url))` for a same-URL
        refetch, or build your own downloader call for the full method/
        headers/body."""
        return curl_to_request(curl_command)

    response = asyncio.run(_fetch(url))
    namespace = {
        "response": response,
        "r": response,
        "fetch": fetch,
        "view": view,
        "from_curl": from_curl,
        "Request": Request,
        "Settings": Settings,
    }
    console.print(
        f"[green]Fetched[/green] {url} "
        f"[dim](status={response.status}, {len(response.body)} bytes)[/dim]"
    )
    console.print("[dim]Variables: response / r, fetch(url), view(), from_curl(cmd)[/dim]")

    try:
        from IPython import start_ipython

        start_ipython(argv=[], user_ns=namespace, display_banner=False)
    except ImportError:
        console.print(
            "[yellow]IPython not installed — falling back to the standard Python "
            "shell. Install with `pip install bitscrape[cli]` for the full "
            "interactive experience (tab completion, syntax highlighting).[/yellow]"
        )
        import code

        code.interact(local=namespace)


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("url")
@click.option("--playwright", is_flag=True, help="Render with a headless browser first.")
@click.option(
    "--as",
    "output_as",
    default="markdown",
    type=click.Choice(["markdown", "html", "text", "structured"]),
    help="What to print: clean Markdown (default), raw HTML, plain text, or "
    "structured data (JSON-LD/Microdata/RDFa/OpenGraph) as JSON.",
)
@click.option("--css", default=None, help="CSS selector to extract instead of the whole page.")
def get(url: str, playwright: bool, output_as: str, css: str | None) -> None:
    """Fetch URL and print the result -- no spider, no shell, just the
    content. `bitscrape get https://example.com --css 'h1::text'`"""
    from bitscrape.core.models import Request
    from bitscrape.core.settings import Settings
    from bitscrape.downloader.downloader import Downloader
    from bitscrape.markdown import html_to_markdown
    from bitscrape.parser.selector import ParsedResponse
    from bitscrape.parser.structured import extract_structured

    async def _run() -> None:
        downloader = Downloader(Settings())
        await downloader.open()
        try:
            response = await downloader.fetch(Request(url=url, use_playwright=playwright))
        finally:
            await downloader.close()

        if css:
            parsed = ParsedResponse(response)
            for value in parsed.css(css).getall():
                print(value)
            return

        if output_as == "html":
            print(response.text)
        elif output_as == "text":
            import re

            from bitscrape.content import find_main_content, parse_html, strip_noise

            root = strip_noise(parse_html(response.body))
            main = find_main_content(root)
            text = re.sub(r"\s+", " ", " ".join(main.itertext())).strip()
            print(text)
        elif output_as == "structured":
            import json

            data = extract_structured(response.body, base_url=response.url)
            print(json.dumps(data.to_dict(), indent=2, ensure_ascii=False))
        else:
            print(html_to_markdown(response.body, base_url=response.url))

    asyncio.run(_run())


@cli.command()
@click.option(
    "--transport",
    default="stdio",
    type=click.Choice(["stdio", "sse", "streamable-http"]),
    help="stdio is what Claude Desktop/Cursor expect in their MCP server config.",
)
@click.option("--name", default="bitscrape", help="Server name reported to the MCP client.")
@click.option(
    "--host", default="127.0.0.1", show_default=True, help="HTTP transports: bind address."
)
@click.option("--port", default=8000, show_default=True, type=int, help="HTTP transports: port.")
@click.option(
    "--auth-token",
    envvar="BITSCRAPE_MCP_TOKEN",
    default=None,
    help="HTTP transports: require 'Authorization: Bearer <token>' (or set BITSCRAPE_MCP_TOKEN). "
    "Required to bind a non-loopback host.",
)
@click.option(
    "--cdp-url", default=None, help="Attach browser sessions to a running browser over CDP."
)
@click.option(
    "--allow-private-networks", is_flag=True, help="Permit loopback/private/link-local targets."
)
@click.option(
    "--no-block-ads", is_flag=True, help="Don't block ad/tracker domains in browser sessions."
)
@click.option("--max-sessions", default=4, show_default=True, type=int)
def mcp(
    transport: str,
    name: str,
    host: str,
    port: int,
    auth_token: str | None,
    cdp_url: str | None,
    allow_private_networks: bool,
    no_block_ads: bool,
    max_sessions: int,
) -> None:
    """Run bitscrape's MCP server (fetch / extract / browser sessions / network
    history tools for AI agents -- see bitscrape.mcp_server)."""
    try:
        from bitscrape.mcp_server import build_server, run_http
    except ImportError:
        console.print(
            "[red]The `mcp` package isn't installed.[/red] Install with "
            "`pip install bitscrape[mcp]`."
        )
        raise SystemExit(1) from None

    server = build_server(
        name=name,
        cdp_url=cdp_url,
        allow_private_networks=allow_private_networks,
        block_ads=not no_block_ads,
        max_sessions=max_sessions,
    )
    if transport == "stdio":
        server.run(transport="stdio")
        return
    try:
        run_http(server, host=host, port=port, token=auth_token, transport=transport)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(2) from None


# ---------------------------------------------------------------------------
# blocklist
# ---------------------------------------------------------------------------


@cli.group()
def blocklist() -> None:
    """Manage the ad/tracker domain list used by block_ads."""


@blocklist.command("update")
@click.option(
    "--mode", type=click.Choice(["union", "intersection"]), default="union", show_default=True
)
@click.option(
    "--dest", default=None, help="Output file (default ~/.cache/bitscrape/ad_domains.txt)."
)
def blocklist_update(mode: str, dest: str | None) -> None:
    """Download community lists and save the merged ad/tracker domain list."""
    from bitscrape.downloader.blocking import update_blocklist, user_blocklist_path

    try:
        n = update_blocklist(mode=mode, dest=dest)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Update failed:[/red] {exc}")
        raise SystemExit(1) from None
    console.print(f"[green]Saved {n:,} domains[/green] to {dest or user_blocklist_path()}")


@blocklist.command("info")
def blocklist_info() -> None:
    """Show how many domains block_ads currently uses, and from where."""
    from bitscrape.downloader.blocking import load_ad_domains, user_blocklist_path

    p = user_blocklist_path()
    src = str(p) if p.is_file() else "bundled core list"
    console.print(f"{len(load_ad_domains()):,} domains ({src})")


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
        spec.loader.exec_module(mod)
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
from bitscrape.spiders import SitemapSpider
from bitscrape.core.models import Response


class {name}Spider(SitemapSpider):
    name = "{name}"
    sitemap_urls = ["https://{domain}/sitemap.xml"]
    # Route matched URLs to different callbacks by substring, e.g.:
    # sitemap_rules = [("/product/", "parse_product")]

    async def parse(self, response: Response):
        yield {{"url": response.url, "title": response.css("title::text").get()}}
"""

_XMLFEED_TEMPLATE = """\
from bitscrape.spiders import XMLFeedSpider


class {name}Spider(XMLFeedSpider):
    name = "{name}"
    start_urls = ["https://{domain}/rss.xml"]
    itertag = "item"  # "entry" for Atom feeds

    async def parse_node(self, response, node):
        yield {{"title": node.findtext("title"), "link": node.findtext("link")}}
"""

_CSVFEED_TEMPLATE = """\
from bitscrape.spiders import CSVFeedSpider


class {name}Spider(CSVFeedSpider):
    name = "{name}"
    start_urls = ["https://{domain}/feed.csv"]
    # headers = ["sku", "price"]  # only needed if the CSV has no header row

    async def parse_row(self, response, row):
        yield row
"""

_SHOPIFY_TEMPLATE = """\
from bitscrape.spiders import ShopifySpider


class {name}Spider(ShopifySpider):
    name = "{name}"
    shop_urls = ["https://{domain}"]

    # Override to customise the item shape (default: one flat dict per
    # variant, with the shared product fields plus that variant's own):
    # async def parse_variant(self, response, product, variant):
    #     yield {{"sku": variant.get("sku"), "price": variant.get("price")}}
"""
