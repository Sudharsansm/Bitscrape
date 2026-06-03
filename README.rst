|logo|

.. |logo| image:: docs/_static/logo.png
   :target: https://github.com/Sudharsansm/bitscrape
   :alt: Bitscrape
   :width: 480px

|version| |python_version| |ubuntu| |macos| |windows| |coverage| |license| |downloads|

.. |version| image:: https://img.shields.io/pypi/v/bitscrape.svg
   :target: https://pypi.org/project/bitscrape/
   :alt: PyPI Version

.. |python_version| image:: https://img.shields.io/pypi/pyversions/bitscrape.svg
   :target: https://pypi.org/project/bitscrape/
   :alt: Supported Python Versions

.. |ubuntu| image:: https://github.com/Sudharsansm/bitscrape/workflows/Ubuntu/badge.svg
   :target: https://github.com/Sudharsansm/bitscrape/actions?query=workflow%3AUbuntu
   :alt: Ubuntu

.. |macos| image:: https://github.com/Sudharsansm/bitscrape/workflows/macOS/badge.svg
   :target: https://github.com/Sudharsansm/bitscrape/actions?query=workflow%3AmacOS
   :alt: macOS

.. |windows| image:: https://github.com/Sudharsansm/bitscrape/workflows/Windows/badge.svg
   :target: https://github.com/Sudharsansm/bitscrape/actions?query=workflow%3AWindowsc:\Users\sudha\Downloads\bitscrape.png
   :alt: Windows

.. |coverage| image:: https://img.shields.io/codecov/c/github/Sudharsansm/bitscrape/main.svg
   :target: https://codecov.io/github/Sudharsansm/bitscrape?branch=main
   :alt: Coverage report

.. |license| image:: https://img.shields.io/pypi/l/bitscrape.svg
   :target: https://github.com/Sudharsansm/bitscrape/blob/main/LICENSE
   :alt: License

.. |downloads| image:: https://img.shields.io/pypi/dm/bitscrape.svg
   :target: https://pypi.org/project/bitscrape/
   :alt: Monthly Downloads

`Bitscrape`_ is a modern, production-grade async web scraping framework for
extracting structured data from websites at scale.

It is cross-platform, requires Python 3.11+, and is built on ``asyncio``,
`Pydantic`_ v2, and `Playwright`_ for JavaScript rendering. It is maintained
by `its contributors`_.

.. _Bitscrape: https://github.com/Sudharsansm/bitscrape
.. _Pydantic: https://docs.pydantic.dev/
.. _Playwright: https://playwright.dev/python/
.. _its contributors: https://github.com/Sudharsansm/bitscrape/graphs/contributors

Overview
========

Bitscrape gives you everything you need to build and run production scrapers:

* **Async by default** — built on ``asyncio`` and ``aiohttp`` for thousands of
  concurrent requests with minimal overhead
* **Type-safe data** — all items, requests, responses, and settings are
  `Pydantic`_ v2 models; no silent type errors in your pipeline
* **JavaScript rendering** — built-in `Playwright`_ support for SPAs and
  dynamic pages; no plugin required
* **Single import** — the entire framework is accessible from ``import bitscrape``
* **Distributed mode** — switch from in-memory to Redis queue with one
  environment variable for horizontal scaling across workers
* **Familiar API** — spider class, CSS/XPath selectors, item pipelines, and
  a ``bitscrape`` CLI that will feel natural to anyone who has used Scrapy

Install
=======

.. code:: bash

    pip install bitscrape

With JavaScript rendering support:

.. code:: bash

    pip install "bitscrape[playwright]"
    playwright install chromium

With distributed (Redis) mode:

.. code:: bash

    pip install "bitscrape[redis]"

Install everything:

.. code:: bash

    pip install "bitscrape[full]"

Or with ``uv``:

.. code:: bash

    uv add bitscrape

Quickstart
==========

Define a spider and run it in under five minutes:

.. code:: python

    import bitscrape

    class QuotesSpider(bitscrape.Spider):
        name = "quotes"
        start_urls = ["https://quotes.toscrape.com/"]

        async def parse(self, response):
            for quote in response.css("div.quote"):
                yield {
                    "text":   quote.css("span.text::text").get(),
                    "author": quote.css("small.author::text").get(),
                    "tags":   quote.css("div.tags a.tag::text").getall(),
                }
            next_page = response.css("li.next a::attr(href)").get()
            if next_page:
                yield self.follow(f"https://quotes.toscrape.com{next_page}")

    if __name__ == "__main__":
        stats = bitscrape.run(QuotesSpider, output="quotes.jsonl")
        print(f"Scraped {stats.items_scraped} quotes in {stats.elapsed:.1f}s")

Or run from the command line:

.. code:: bash

    bitscrape crawl quotes_spider.py -o quotes.jsonl

Type-safe items with `Pydantic`_:

.. code:: python

    import bitscrape

    class QuoteItem(bitscrape.Item):
        text:   str
        author: str
        tags:   list[str] = []

    class QuotesSpider(bitscrape.Spider):
        name = "quotes"
        start_urls = ["https://quotes.toscrape.com/"]

        async def parse(self, response):
            for quote in response.css("div.quote"):
                yield QuoteItem(
                    text=quote.css("span.text::text").get(default=""),
                    author=quote.css("small.author::text").get(default=""),
                    tags=quote.css("div.tags a.tag::text").getall(),
                )

CLI Commands
============

.. code:: bash

    bitscrape startproject myproject      # scaffold a new project
    bitscrape genspider myspider site.com # generate a spider template
    bitscrape crawl myspider.py -o out.jsonl  # run a spider
    bitscrape list                        # list all spiders in project

Export Formats
==============

.. code:: bash

    bitscrape crawl myspider.py -o data.jsonl   # JSON Lines (recommended)
    bitscrape crawl myspider.py -o data.json    # JSON array
    bitscrape crawl myspider.py -o data.csv     # CSV spreadsheet
    bitscrape crawl myspider.py -o data.xml     # XML

Distributed Mode
================

Share a Redis queue across multiple worker processes for horizontal scaling:

.. code:: bash

    export BITSCRAPE_SCHEDULER_USE_REDIS=true
    export BITSCRAPE_REDIS_URL=redis://localhost:6379/0

    # Start as many workers as needed — they all share the queue
    bitscrape crawl myspider.py &
    bitscrape crawl myspider.py &
    bitscrape crawl myspider.py &

JavaScript Rendering
====================

Mark individual requests to be rendered by `Playwright`_ (Chromium):

.. code:: python

    async def parse(self, response):
        # Follow a JavaScript-rendered page
        yield self.follow("/dynamic-page", use_playwright=True)

Requirements
============

* Python 3.11+
* Works on Linux, macOS, and Windows
* Core dependencies: ``aiohttp``, ``pydantic``, ``selectolax``, ``click``, ``rich``
* Optional: ``playwright`` (JS rendering), ``redis`` (distributed), ``asyncpg`` (PostgreSQL)

Contributing
============

Contributions are welcome! Please read `CONTRIBUTING.md`_ before submitting a
pull request.

.. _CONTRIBUTING.md: https://github.com/Sudharsansm/bitscrape/blob/main/CONTRIBUTING.md

To set up a development environment:

.. code:: bash

    git clone https://github.com/Sudharsansm/bitscrape.git
    cd bitscrape
    pip install -e ".[dev]"
    pytest tests/

License
=======

Bitscrape is distributed under the `MIT License`_.

.. _MIT License: https://github.com/Sudharsansm/bitscrape/blob/main/LICENSE
