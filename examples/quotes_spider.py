"""
Example: quotes.toscrape.com
Run: bitscrape crawl examples/quotes_spider.py -o quotes.jsonl
  or: python examples/quotes_spider.py
"""
import bitscrape


class QuoteItem(bitscrape.Item):
    text: str
    author: str
    tags: list[str] = []


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
        nxt = response.css("li.next a::attr(href)").get()
        if nxt:
            yield self.follow(f"https://quotes.toscrape.com{nxt}")


if __name__ == "__main__":
    stats = bitscrape.run(QuotesSpider, output="quotes.jsonl")
    print(f"\nDone! {stats.items_scraped} quotes scraped.")
