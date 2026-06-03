"""
Example: books.toscrape.com — full pipeline demo.
Run: python examples/ecommerce_spider.py
"""

import bitscrape


class BookItem(bitscrape.Item):
    title: str
    price: float
    in_stock: bool = True


class BooksSpider(bitscrape.Spider):
    name = "books"
    start_urls = ["https://books.toscrape.com/"]

    async def parse(self, response):
        for book in response.css("article.product_pod"):
            raw_price = book.css("p.price_color::text").get(default="0")
            try:
                price = float(raw_price.replace("£", "").replace("Â", "").strip())
            except ValueError:
                price = 0.0

            yield BookItem(
                title=book.css("h3 a::attr(title)").get(default="Unknown"),
                price=price,
                in_stock="In stock" in (book.css("p.availability::text").get() or ""),
            )

        nxt = response.css("li.next a::attr(href)").get()
        if nxt:
            from urllib.parse import urljoin

            yield self.follow(urljoin(response.url, nxt))


if __name__ == "__main__":
    stats = bitscrape.run(
        BooksSpider,
        output="books.jsonl",
        pipelines=[
            bitscrape.ValidationPipeline(),
            bitscrape.DedupPipeline(),
            bitscrape.LoggingPipeline(),
        ],
        settings=bitscrape.Settings(concurrent_requests=8, robotstxt_obey=False),
    )
    print(f"\nDone! {stats.items_scraped} books scraped in {stats.elapsed:.1f}s")
