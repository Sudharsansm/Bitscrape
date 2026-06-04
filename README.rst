|logo|

.. |logo| image:: https://raw.githubusercontent.com/Sudharsansm/Bitscrape/main/docs/bitscrape.png
:target: https://github.com/Sudharsansm/Bitscrape
:alt: Bitscrape
:width: 480px

|version| |python_version| |license|

.. |version| image:: https://img.shields.io/pypi/v/bitscrape.svg
:target: https://pypi.org/project/bitscrape/
:alt: PyPI Version

.. |python_version| image:: https://img.shields.io/pypi/pyversions/bitscrape.svg
:target: https://pypi.org/project/bitscrape/
:alt: Supported Python Versions

.. |license| image:: https://img.shields.io/pypi/l/bitscrape.svg
:target: https://github.com/Sudharsansm/Bitscrape/blob/main/LICENSE
:alt: License

# Bitscrape

Bitscrape is a modern, production-grade asynchronous web scraping framework
built for high-performance crawling, structured data extraction, and browser
automation.

It provides fast async networking, powerful HTML parsing, configurable
pipelines, distributed crawling support, and optional browser rendering with
Playwright.

# Features

* Async-first architecture built on `asyncio`
* High-performance HTTP crawling
* CSS and XPath selectors
* Type-safe models powered by Pydantic v2
* Built-in Playwright support
* Redis-based distributed crawling
* Item pipelines and exporters
* JSON, JSONL, CSV, and XML output
* Cross-platform support
* Rich CLI experience

# Install

Using pip:

.. code:: bash

pip install bitscrape

Using uv:

.. code:: bash

uv add bitscrape

Playwright support:

.. code:: bash

pip install "bitscrape[playwright]"

playwright install chromium

# Documentation

* GitHub Repository:
  https://github.com/Sudharsansm/Bitscrape

* Issue Tracker:
  https://github.com/Sudharsansm/Bitscrape/issues

# Run

.. code:: bash

bitscrape crawl examples/quotes_spider.py -o quotes.jsonl

# License

Bitscrape is licensed under the MIT License.