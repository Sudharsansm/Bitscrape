# Parser

Extracting data from a `Response` via CSS or XPath selectors.

## Basic usage

Every `Response` (as received in your spider's callback) supports both:

```python
async def parse(self, response):
    # CSS
    title = response.css("h1::text").get()
    all_links = response.css("a::attr(href)").getall()

    # XPath
    title = response.xpath("//h1/text()").get()
    all_links = response.xpath("//a/@href").getall()
```

- `.get(default=None)` — the first match, or `default` if there are none.
- `.getall()` — every match, as a list (empty list if none).

## CSS pseudo-selectors

- `::text` — the text content of the element.
- `::attr(name)` — the value of the given attribute (e.g. `::attr(href)`,
  `::attr(src)`).

## Chaining selectors

`.css()` returns a `SelectorList`; you can call `.css()` again on it to
scope further, or iterate it to work with individual matched elements:

```python
for card in response.css("div.quote"):
    text = card.css("span.text::text").get()
    author = card.css("small.author::text").get()
    yield {"text": text, "author": author}
```

## Choosing CSS vs. XPath

CSS selectors are more concise for the common cases (classes, IDs, simple
attribute matches). XPath is more powerful for things CSS can't express
directly — selecting an element by its text content, navigating to a
parent/ancestor, or complex conditional logic:

```python
# "Find the <a> tag whose text is exactly 'Next'"
response.xpath("//a[text()='Next']/@href").get()

# "Find the parent div of a span with class 'price'"
response.xpath("//span[@class='price']/..").get()
```

## Debugging a selector that returns nothing

If `.get()`/`.getall()` returns `None`/`[]` when you expected a match:

1. **Print `response.text`** (or save `response.body` to a file and open it
   in a browser) — confirm the HTML you're selecting against actually
   contains what you think it does. A very common cause: the content is
   populated by JavaScript and isn't in the raw HTML at all — see
   [browser/](../browser/index.md).
2. **Check for a redirect** — `response.url` might not be the URL you
   requested if the server redirected.
3. **Simplify the selector** — start with something broad
   (`response.css("div").getall()` just to see if *any* divs match) and
   narrow down from there.

See [troubleshooting/](../troubleshooting/index.md) for the Engine's
built-in diagnostic that fires automatically when a callback yields nothing
at all.

## See also

- [extractors/](../extractors/index.md) — structured extraction beyond raw CSS/XPath (canonicalization, entity resolution).
- [api/index.md#parser-bitscrapeparserselector](../api/index.md#parser-bitscrapeparserselector) — full signatures.
