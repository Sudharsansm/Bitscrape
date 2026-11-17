"""
End-to-end CLI tests using click's CliRunner, invoking the actual
`bitscrape` command entry points -- not just unit-testing internal
functions. Closes the coverage gap flagged in BITSCRAPE_QA_REPORT.md
("cli/main.py -- 36% coverage; the crawl/list/genspider/startproject
subcommands aren't exercised end-to-end").

Uses a plain threading + http.server based local server (not
aiohttp.test_utils.TestServer) for the `crawl` tests, since `crawl`
internally calls asyncio.run() itself (via CliRunner.invoke(), which runs
synchronously) -- the same event-loop-coupling issue documented in
tests/test_package_api.py applies here too.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from click.testing import CliRunner

from bitscrape.cli.main import cli


class _ThreadedTestServer:
    """Loop-independent local HTTP server -- see test_package_api.py for
    why this is needed instead of aiohttp's TestServer for CLI tests."""

    def __init__(self, body: bytes = b"<html><body>hello</body></html>"):
        self.body = body
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(outer.body)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                pass

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address
        return f"http://{host}:{port}/page"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2)


def _write_spider_file(path: Path, url: str) -> None:
    path.write_text(
        f"""
from bitscrape.core.spider import Spider

class CliTestSpider(Spider):
    name = "cli_test_spider"
    start_urls = ["{url}"]

    async def parse(self, response):
        yield {{"body_len": len(response.body)}}
"""
    )


# ---------------------------------------------------------------------------
# bitscrape crawl
# ---------------------------------------------------------------------------


def test_crawl_writes_jsonl_output(tmp_path):
    server = _ThreadedTestServer()
    try:
        spider_file = tmp_path / "spider.py"
        _write_spider_file(spider_file, server.url)
        output_file = tmp_path / "out.jsonl"

        runner = CliRunner()
        result = runner.invoke(
            cli, ["crawl", str(spider_file), "-o", str(output_file), "--no-robots"]
        )

        assert result.exit_code == 0, result.output
        assert "Crawl Stats" in result.output
        assert output_file.exists()
        lines = output_file.read_text().strip().splitlines()
        assert len(lines) == 1
        item = json.loads(lines[0])
        assert item["body_len"] > 0
    finally:
        server.close()


def test_crawl_writes_csv_output_with_fmt_flag(tmp_path):
    server = _ThreadedTestServer()
    try:
        spider_file = tmp_path / "spider.py"
        _write_spider_file(spider_file, server.url)
        output_file = tmp_path / "out.csv"

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["crawl", str(spider_file), "-o", str(output_file), "--fmt", "csv", "--no-robots"],
        )

        assert result.exit_code == 0, result.output
        assert output_file.exists()
        content = output_file.read_text()
        assert "body_len" in content
    finally:
        server.close()


def test_crawl_respects_concurrency_flag(tmp_path):
    server = _ThreadedTestServer()
    try:
        spider_file = tmp_path / "spider.py"
        _write_spider_file(spider_file, server.url)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["crawl", str(spider_file), "--concurrency", "4", "--no-robots"],
        )
        assert result.exit_code == 0, result.output
        assert "Requests" in result.output
    finally:
        server.close()


def test_crawl_reports_stats_summary_fields(tmp_path):
    server = _ThreadedTestServer()
    try:
        spider_file = tmp_path / "spider.py"
        _write_spider_file(spider_file, server.url)

        runner = CliRunner()
        result = runner.invoke(cli, ["crawl", str(spider_file), "--no-robots"])

        assert result.exit_code == 0, result.output
        for field in ["Requests", "Failed", "Items scraped", "Items dropped"]:
            assert field in result.output
    finally:
        server.close()


def test_crawl_nonexistent_spider_file_fails_gracefully():
    runner = CliRunner()
    result = runner.invoke(cli, ["crawl", "/nonexistent/path/spider.py"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# bitscrape list
# ---------------------------------------------------------------------------


def test_list_spiders_shows_files_in_directory(tmp_path):
    spiders_dir = tmp_path / "spiders"
    spiders_dir.mkdir()
    (spiders_dir / "quotes.py").write_text("# demo")
    (spiders_dir / "news.py").write_text("# demo")
    (spiders_dir / "__init__.py").write_text("")

    runner = CliRunner()
    result = runner.invoke(cli, ["list", "--dir", str(spiders_dir)])

    assert result.exit_code == 0, result.output
    assert "quotes" in result.output
    assert "news" in result.output
    assert "__init__" not in result.output


def test_list_spiders_missing_directory_reports_clearly(tmp_path):
    runner = CliRunner()
    result = runner.invoke(cli, ["list", "--dir", str(tmp_path / "does-not-exist")])
    assert "No spiders directory found" in result.output


def test_list_spiders_empty_directory(tmp_path):
    spiders_dir = tmp_path / "spiders"
    spiders_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["list", "--dir", str(spiders_dir)])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# bitscrape genspider
# ---------------------------------------------------------------------------


def test_genspider_creates_basic_template(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["genspider", "quotes", "example.com"])

    assert result.exit_code == 0, result.output
    spider_file = tmp_path / "spiders" / "quotes.py"
    assert spider_file.exists()
    content = spider_file.read_text()
    assert "class" in content
    assert "example.com" in content
    assert "async def parse" in content


def test_genspider_creates_crawl_template(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["genspider", "articles", "example.com", "--template", "crawl"]
    )
    assert result.exit_code == 0, result.output
    content = (tmp_path / "spiders" / "articles.py").read_text()
    assert "follow" in content


def test_genspider_creates_sitemap_template(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["genspider", "sitemapper", "example.com", "--template", "sitemap"]
    )
    assert result.exit_code == 0, result.output
    content = (tmp_path / "spiders" / "sitemapper.py").read_text()
    assert "sitemap" in content.lower()


def test_genspider_the_generated_file_is_actually_valid_python(tmp_path, monkeypatch):
    """Not just 'a file was written' -- confirm it's syntactically valid,
    so genspider's output is genuinely usable."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["genspider", "realcheck", "example.com"])
    assert result.exit_code == 0, result.output

    spider_file = tmp_path / "spiders" / "realcheck.py"
    compile(spider_file.read_text(), str(spider_file), "exec")


def test_genspider_creates_xmlfeed_template(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["genspider", "newsfeed", "example.com", "--template", "xmlfeed"]
    )
    assert result.exit_code == 0, result.output
    spider_file = tmp_path / "spiders" / "newsfeed.py"
    content = spider_file.read_text()
    assert "XMLFeedSpider" in content
    compile(content, str(spider_file), "exec")


def test_genspider_creates_csvfeed_template(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["genspider", "prices", "example.com", "--template", "csvfeed"]
    )
    assert result.exit_code == 0, result.output
    spider_file = tmp_path / "spiders" / "prices.py"
    content = spider_file.read_text()
    assert "CSVFeedSpider" in content
    compile(content, str(spider_file), "exec")


def test_genspider_creates_shopify_template(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["genspider", "myshop", "myshop.myshopify.com", "--template", "shopify"]
    )
    assert result.exit_code == 0, result.output
    spider_file = tmp_path / "spiders" / "myshop.py"
    content = spider_file.read_text()
    assert "ShopifySpider" in content
    assert "myshop.myshopify.com" in content
    compile(content, str(spider_file), "exec")


def test_genspider_updated_sitemap_template_uses_sitemapspider(tmp_path, monkeypatch):
    """The sitemap template used to hand-roll `loc::text` CSS on the raw
    XML (no sitemap-index or gzip support); it now generates a real
    SitemapSpider subclass."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["genspider", "sitemapper2", "example.com", "--template", "sitemap"]
    )
    assert result.exit_code == 0, result.output
    content = (tmp_path / "spiders" / "sitemapper2.py").read_text()
    assert "SitemapSpider" in content
    compile(content, str((tmp_path / "spiders" / "sitemapper2.py")), "exec")


# ---------------------------------------------------------------------------
# bitscrape shell
# ---------------------------------------------------------------------------


def test_shell_fetches_and_reports_status(monkeypatch):
    """The shell command fetches the URL and reports status before handing
    off to an interactive prompt -- verify the fetch + report half without
    actually needing a real interactive TTY. We stub out the interactive
    part (IPython/code.interact) since CliRunner has no real terminal."""
    server = _ThreadedTestServer(body=b"hello from shell test")
    try:
        import bitscrape.cli.main as main_module

        started = {}

        def fake_start_ipython(argv, user_ns, display_banner):
            started["called"] = True
            started["namespace"] = user_ns

        monkeypatch.setattr(
            "IPython.start_ipython", fake_start_ipython, raising=False
        )
        # IPython may not be importable at all in some environments -- patch
        # the import site bitscrape actually uses.
        import types

        fake_ipython_module = types.SimpleNamespace(start_ipython=fake_start_ipython)
        monkeypatch.setitem(__import__("sys").modules, "IPython", fake_ipython_module)

        runner = CliRunner()
        result = runner.invoke(main_module.cli, ["shell", server.url])

        assert result.exit_code == 0, result.output
        assert "Fetched" in result.output
        assert started.get("called") is True
        assert "response" in started["namespace"]
        assert started["namespace"]["response"].status == 200
        assert started["namespace"]["response"].body == b"hello from shell test"
    finally:
        server.close()


def test_shell_namespace_includes_view_and_from_curl(monkeypatch):
    """view() and from_curl() -- the shell's answers to Scrapling's "view
    results in your browser" and "convert curl to a request" features."""
    server = _ThreadedTestServer(body=b"<html><body>hi</body></html>")
    try:
        import bitscrape.cli.main as main_module
        import types

        started = {}

        def fake_start_ipython(argv, user_ns, display_banner):
            started["namespace"] = user_ns

        fake_ipython_module = types.SimpleNamespace(start_ipython=fake_start_ipython)
        monkeypatch.setitem(__import__("sys").modules, "IPython", fake_ipython_module)

        opened = {}
        monkeypatch.setattr(
            "webbrowser.open", lambda target: opened.setdefault("target", target)
        )

        runner = CliRunner()
        result = runner.invoke(main_module.cli, ["shell", server.url])
        assert result.exit_code == 0, result.output

        namespace = started["namespace"]
        assert "view" in namespace
        assert "from_curl" in namespace

        namespace["view"]()
        assert opened["target"].startswith("file://")

        req = namespace["from_curl"]("curl 'https://example.com/x' -H 'Accept: text/html'")
        assert req.url == "https://example.com/x"
        assert req.headers["Accept"] == "text/html"
    finally:
        server.close()


def test_shell_falls_back_to_stdlib_shell_without_ipython(monkeypatch):
    server = _ThreadedTestServer()
    try:
        import bitscrape.cli.main as main_module

        # Simulate IPython not being installed.
        monkeypatch.setitem(__import__("sys").modules, "IPython", None)

        interacted = {}

        def fake_interact(local):
            interacted["local"] = local

        monkeypatch.setattr("code.interact", fake_interact)

        runner = CliRunner()
        result = runner.invoke(main_module.cli, ["shell", server.url])

        assert result.exit_code == 0, result.output
        assert "IPython not installed" in result.output
        assert interacted.get("local") is not None
        assert "response" in interacted["local"]
    finally:
        server.close()


# ---------------------------------------------------------------------------
# bitscrape startproject
# ---------------------------------------------------------------------------


def test_startproject_creates_expected_structure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["startproject", "myproject"])

    assert result.exit_code == 0, result.output
    project_dir = tmp_path / "myproject"
    assert project_dir.is_dir()
    assert (project_dir / "spiders" / "__init__.py").exists()
    assert (project_dir / "pipelines" / "__init__.py").exists()
    assert (project_dir / "items" / "__init__.py").exists()
    assert (project_dir / "settings.py").exists()
    assert (project_dir / "scrapy.cfg").exists()
    assert (project_dir / "README.md").exists()


def test_startproject_settings_file_is_valid_python(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(cli, ["startproject", "myproject2"])
    settings_file = tmp_path / "myproject2" / "settings.py"
    compile(settings_file.read_text(), str(settings_file), "exec")


def test_startproject_fails_if_directory_already_exists(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "existing").mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["startproject", "existing"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------


def test_cli_help_lists_all_commands():
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    for command in ["crawl", "list", "genspider", "startproject", "get", "shell"]:
        assert command in result.output


def test_cli_version_flag():
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "bitscrape" in result.output.lower()


# ---------------------------------------------------------------------------
# get (one-shot fetch, no spider/shell needed)
# ---------------------------------------------------------------------------

_GET_TEST_HTML = (
    b"<html><body><h1>Hello World</h1><p>Some real content here.</p>"
    b'<script type="application/ld+json">{"@type": "Thing", "name": "X"}</script>'
    b"</body></html>"
)


def test_get_default_prints_markdown():
    server = _ThreadedTestServer(body=_GET_TEST_HTML)
    try:
        runner = CliRunner()
        result = runner.invoke(cli, ["get", server.url])
        assert result.exit_code == 0, result.output
        assert "# Hello World" in result.output
        assert "Some real content here." in result.output
    finally:
        server.close()


def test_get_as_html_prints_raw_markup():
    server = _ThreadedTestServer(body=_GET_TEST_HTML)
    try:
        runner = CliRunner()
        result = runner.invoke(cli, ["get", server.url, "--as", "html"])
        assert result.exit_code == 0, result.output
        assert "<h1>Hello World</h1>" in result.output
    finally:
        server.close()


def test_get_as_text_strips_tags():
    server = _ThreadedTestServer(body=_GET_TEST_HTML)
    try:
        runner = CliRunner()
        result = runner.invoke(cli, ["get", server.url, "--as", "text"])
        assert result.exit_code == 0, result.output
        assert "<h1>" not in result.output
        assert "Hello World" in result.output
        assert "Some real content here." in result.output
    finally:
        server.close()


def test_get_as_structured_prints_json_ld():
    server = _ThreadedTestServer(body=_GET_TEST_HTML)
    try:
        runner = CliRunner()
        result = runner.invoke(cli, ["get", server.url, "--as", "structured"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["json_ld"][0]["name"] == "X"
    finally:
        server.close()


def test_get_with_css_selector_extracts_only_that():
    server = _ThreadedTestServer(body=_GET_TEST_HTML)
    try:
        runner = CliRunner()
        result = runner.invoke(cli, ["get", server.url, "--css", "h1::text"])
        assert result.exit_code == 0, result.output
        assert result.output.strip() == "Hello World"
        assert "Some real content" not in result.output
    finally:
        server.close()
