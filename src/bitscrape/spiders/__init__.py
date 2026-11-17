"""
Ready-made spider templates -- see ``bitscrape.spiders.templates`` and
``bitscrape.crawl`` for each class's docstring and an example.

- :class:`CrawlSpider` + :class:`Rule` + :class:`LinkExtractor` -- rule-based
  link following (re-exported here from ``bitscrape.crawl`` so every template
  lives under one import path).
- :class:`SitemapSpider` -- sitemap- and robots.txt-driven crawls.
- :class:`XMLFeedSpider` / :class:`CSVFeedSpider` -- iterate XML/RSS/Atom and
  CSV feeds.
- :class:`ShopifySpider` -- every product (and variant) from any Shopify
  storefront's JSON API.
- :class:`SiteToMarkdownSpider` -- crawl a site into clean, LLM-ready Markdown.
"""

from __future__ import annotations

from bitscrape.crawl import CrawlSpider, Link, LinkExtractor, Rule
from bitscrape.spiders.templates import (
    ContentSpider,
    CSVFeedSpider,
    ShopifySpider,
    SitemapSpider,
    SiteToMarkdownSpider,
    XMLFeedSpider,
)

__all__ = [
    "CSVFeedSpider",
    "ContentSpider",
    "CrawlSpider",
    "Link",
    "LinkExtractor",
    "Rule",
    "ShopifySpider",
    "SiteToMarkdownSpider",
    "SitemapSpider",
    "XMLFeedSpider",
]
