"""
Example: an infinite-scroll listing page crawled through Playwright, with
proxy rotation enabled.

Demonstrates the two new middleware/downloader features added in this patch:
  - ProxyMiddleware  (bitscrape.middleware.middleware.ProxyMiddleware)
  - scroll_to_bottom (bitscrape.downloader.downloader.scroll_to_bottom),
    enabled per-request via request.meta["scroll"]

Run with:
    bitscrape crawl examples/infinite_scroll_spider.py -o listings.jsonl
"""

from __future__ import annotations

from bitscrape.core.models import Response
from bitscrape.core.spider import Spider


class InfiniteScrollSpider(Spider):
    name = "infinite_scroll_demo"
    start_urls = ["https://example.com/listings"]

    custom_settings = {
        "robotstxt_obey": True,
    }

    def start_requests(self):
        for url in self.start_urls:
            yield self.follow(
                url,
                use_playwright=True,
                meta={
                    # True = use scroll_to_bottom()'s defaults.
                    # Or pass overrides, e.g.:
                    # "scroll": {"max_scrolls": 30, "pause_ms": 500,
                    #            "click_selector": "button.load-more"}
                    "scroll": True,
                },
            )

    async def parse(self, response: Response):
        # After scroll_to_bottom() has run, response.body already contains
        # the fully lazy-loaded page -- no extra "next page" request needed
        # for scroll-triggered content.
        for card in response.css("div.listing-card"):
            yield {
                "title": card.css("h2::text").get(),
                "price": card.css(".price::text").get(),
                "url": card.css("a::attr(href)").get(),
            }


# ---------------------------------------------------------------------------
# Wiring ProxyMiddleware in: this normally goes in your project's settings /
# engine setup, shown here for reference.
# ---------------------------------------------------------------------------
#
#   from bitscrape.middleware.middleware import (
#       MiddlewareManager, ProxyMiddleware, RobotsMiddleware, UserAgentMiddleware,
#   )
#
#   proxy_mw = ProxyMiddleware(
#       proxies=[
#           "http://user:pass@proxy1.example.com:8080",
#           "http://user:pass@proxy2.example.com:8080",
#       ],
#       rotate=True,  # random rotation; use rotate=False for round-robin
#   )
#
#   middlewares = MiddlewareManager([
#       UserAgentMiddleware(rotate=True),
#       proxy_mw,
#       RobotsMiddleware(),
#   ])
#
#   # If a proxy starts failing repeatedly, drop it from rotation at runtime:
#   # proxy_mw.remove_proxy("http://user:pass@proxy1.example.com:8080")
