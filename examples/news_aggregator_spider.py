
"""
Bitscrape Multi-Source News Aggregator
======================================

Sources:
    - The Hindu
    - The Indian Express
    - NDTV

Run:

    bitscrape crawl examples/news_aggregator_spider.py -o news.jsonl

This spider is designed for the Bitscrape parser implementation used by
this project.

Important:
    - It uses Playwright for JavaScript-capable pages.
    - It respects robots.txt.
    - It does NOT bypass HTTP 403 responses, CAPTCHAs, paywalls, or
      access controls.
    - Different publishers use different HTML structures, so extraction
      is source-aware.
"""

from __future__ import annotations

from bitscrape.core.models import Response
from bitscrape.core.spider import Spider


class NDTVSpider(Spider):
    name = "ndtv_demo"

    start_urls = [
        "https://www.ndtv.com/"
    ]

    custom_settings = {
        "robotstxt_obey": True,
    }

    def start_requests(self):
        for url in self.start_urls:
            yield self.follow(
                url,
                use_playwright=True,
                meta={
                    "scroll": {
                        "max_scrolls": 5,
                        "pause_ms": 1000,
                    },
                },
            )

    async def parse(self, response: Response):
        print(f"URL: {response.url}")
        print(f"Status: {response.status}")
        print(f"Response size: {len(response.body)} bytes")

        # Inspect links first rather than assuming a listing-card selector.
        for link in response.css("a::attr(href)").getall():
            if link:
                yield {
                    "url": link,
                }