"""
Example: a well-behaved crawler that leans on the fixed RobotsMiddleware.

Before this patch, `robotstxt_obey=True` silently did nothing (see
CHANGELOG.md / README.md for the root-cause `.feed()` vs `.parse()` bug), so
this exact spider would previously have crawled disallowed paths without any
warning. It now genuinely respects robots.txt, and picks up Crawl-delay and
Sitemap entries automatically.

Run with:
    bitscrape crawl examples/polite_sitemap_spider.py -o pages.jsonl
"""

from __future__ import annotations

import asyncio

from bitscrape.core.models import Response
from bitscrape.core.spider import Spider


class PoliteSitemapSpider(Spider):
    name = "polite_sitemap_demo"
    start_urls = ["https://example.com/"]

    custom_settings = {
        "robotstxt_obey": True,  # now actually enforced
        "download_delay": 1.0,  # baseline; overridden per-domain below if needed
    }

    async def parse(self, response: Response):
        # RobotsMiddleware populates these from the real robots.txt --
        # no more hardcoding "/sitemap.xml" yourself.
        sitemaps = response.request.meta.get("sitemaps", [])
        crawl_delay = response.request.meta.get("crawl_delay")

        if crawl_delay:
            self.logger.info(
                "robots.txt requests a %.1fs crawl delay for %s", crawl_delay, response.url
            )
            # Respect it explicitly if it's stricter than our configured delay.
            await asyncio.sleep(max(0.0, crawl_delay - self.settings.download_delay))

        for sitemap_url in sitemaps:
            self.logger.info("Discovered sitemap: %s", sitemap_url)
            yield self.follow(sitemap_url, callback="parse_sitemap")

        for link in response.css("a::attr(href)").getall():
            yield self.follow(link)

    async def parse_sitemap(self, response: Response):
        for loc in response.xpath("//*[local-name()='loc']/text()").getall():
            yield self.follow(loc)
