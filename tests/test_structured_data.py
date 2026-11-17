"""JSON-LD, Microdata, RDFa, OpenGraph, Twitter cards and link relations."""

from __future__ import annotations

from bitscrape.core.models import Request, Response
from bitscrape.parser.selector import ParsedResponse
from bitscrape.parser.structured import StructuredData, extract_structured

PRODUCT_PAGE = """<!doctype html><html><head>
<title>  Trail   Runner Pro | Shop </title>
<meta name="description" content="Lightweight shoe">
<meta property="og:title" content="Trail Runner Pro">
<meta property="og:image" content="https://cdn.example/a.jpg">
<meta property="og:image" content="https://cdn.example/b.jpg">
<meta name="twitter:card" content="summary_large_image">
<link rel="canonical" href="/p/1">
<link rel="alternate" hreflang="de" href="/de/p/1">
<link rel="alternate" type="application/rss+xml" href="/feed.xml">
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "Product", "name": "Trail Runner Pro",
 "offers": {"@type": "Offer", "price": "129.99", "priceCurrency": "USD",},
}
</script>
</head><body></body></html>"""


class TestJsonLd:
    def test_extracts_and_repairs_trailing_commas(self):
        data = extract_structured(PRODUCT_PAGE)
        assert len(data.json_ld) == 1
        assert data.json_ld[0]["offers"]["price"] == "129.99"

    def test_graph_is_flattened_and_inherits_context(self):
        html = """<script type="application/ld+json">
        {"@context":"https://schema.org","@graph":[
           {"@type":"Organization","name":"Acme"},
           {"@type":"WebSite","url":"https://acme.example"}]}</script>"""
        data = extract_structured(f"<html><head>{html}</head></html>")
        assert [d["@type"] for d in data.json_ld] == ["Organization", "WebSite"]
        assert all(d["@context"] == "https://schema.org" for d in data.json_ld)

    def test_array_of_nodes(self):
        html = '<script type="application/ld+json">[{"@type":"A"},{"@type":"B"}]</script>'
        assert [d["@type"] for d in extract_structured(html).json_ld] == ["A", "B"]

    def test_html_comment_and_cdata_wrappers(self):
        html = (
            '<script type="application/ld+json"><!-- {"@type":"A"} --></script>'
            '<script type="application/ld+json">//<![CDATA[\n{"@type":"B"}\n//]]></script>'
        )
        assert [d["@type"] for d in extract_structured(html).json_ld] == ["A", "B"]

    def test_raw_newline_inside_string_is_tolerated(self):
        html = (
            '<script type="application/ld+json">{"@type":"A","description":"line1\nline2"}</script>'
        )
        assert extract_structured(html).json_ld[0]["description"] == "line1\nline2"

    def test_garbage_json_is_skipped_not_fatal(self):
        html = (
            '<script type="application/ld+json">{not json at all</script>'
            '<script type="application/ld+json">{"@type":"Ok"}</script>'
        )
        assert [d["@type"] for d in extract_structured(html).json_ld] == ["Ok"]

    def test_mime_type_is_case_insensitive(self):
        html = '<script type="Application/LD+JSON">{"@type":"A"}</script>'
        assert len(extract_structured(html).json_ld) == 1


class TestMetaAndLinks:
    def test_opengraph_repeats_become_lists(self):
        data = extract_structured(PRODUCT_PAGE, base_url="https://shop.example/p/1")
        assert data.opengraph["og:title"] == "Trail Runner Pro"
        assert data.opengraph["og:image"] == [
            "https://cdn.example/a.jpg",
            "https://cdn.example/b.jpg",
        ]

    def test_twitter_meta_and_title(self):
        data = extract_structured(PRODUCT_PAGE)
        assert data.twitter == {"twitter:card": "summary_large_image"}
        assert data.meta["title"] == "Trail Runner Pro | Shop"
        assert data.meta["description"] == "Lightweight shoe"

    def test_links_are_absolute_and_grouped(self):
        data = extract_structured(PRODUCT_PAGE, base_url="https://shop.example/p/1")
        assert data.links["canonical"][0]["href"] == "https://shop.example/p/1"
        assert data.links["alternate"][0]["hreflang"] == "de"
        assert data.links["alternate"][0]["href"] == "https://shop.example/de/p/1"
        assert data.links["alternate"][1]["type"] == "application/rss+xml"

    def test_base_href_is_honoured(self):
        html = '<base href="https://cdn.example/x/"><link rel="canonical" href="page">'
        data = extract_structured(html, base_url="https://other.example/")
        assert data.links["canonical"][0]["href"] == "https://cdn.example/x/page"


MICRODATA = """<div itemscope itemtype="https://schema.org/Product" itemid="/sku/9">
  <span itemprop="name">Widget</span>
  <img itemprop="image" src="/w.png">
  <meta itemprop="sku" content="W-9">
  <link itemprop="availability" href="https://schema.org/InStock">
  <time itemprop="releaseDate" datetime="2024-05-01">May 1</time>
  <div itemprop="offers" itemscope itemtype="https://schema.org/Offer">
    <span itemprop="price">19.99</span><meta itemprop="priceCurrency" content="EUR">
  </div>
  <span itemprop="review">Great</span><span itemprop="review">Solid</span>
</div>"""


class TestMicrodata:
    def test_scalar_properties_use_html_value_rules(self):
        item = extract_structured(MICRODATA, base_url="https://x.example").microdata[0]
        props = item["properties"]
        assert item["type"] == ["https://schema.org/Product"]
        assert item["id"] == "https://x.example/sku/9"
        assert props["name"] == ["Widget"]
        assert props["image"] == ["https://x.example/w.png"]  # src, made absolute
        assert props["sku"] == ["W-9"]  # meta content
        assert props["availability"] == ["https://schema.org/InStock"]  # link href
        assert props["releaseDate"] == ["2024-05-01"]  # time datetime

    def test_nested_item_and_repeated_property(self):
        props = extract_structured(MICRODATA).microdata[0]["properties"]
        offer = props["offers"][0]
        assert offer["properties"]["price"] == ["19.99"]
        assert offer["properties"]["priceCurrency"] == ["EUR"]
        assert props["review"] == ["Great", "Solid"]
        assert "price" not in props  # nested item's props must not leak upward

    def test_itemref(self):
        html = (
            '<div itemscope itemtype="https://schema.org/Thing" itemref="extra"></div>'
            '<p id="extra" itemprop="name">Referenced</p>'
        )
        assert extract_structured(html).microdata[0]["properties"]["name"] == ["Referenced"]


RDFA = """<div vocab="https://schema.org/" typeof="Recipe">
  <h1 property="name">Pancakes</h1>
  <span property="cookTime" content="PT15M">15 min</span>
  <a property="image" href="/p.jpg">pic</a>
  <div property="author" typeof="Person"><span property="name">Sam</span></div>
</div>"""


class TestRdfa:
    def test_lite_subset(self):
        item = extract_structured(RDFA, base_url="https://r.example").rdfa[0]
        assert item["type"] == ["https://schema.org/Recipe"]
        assert item["properties"]["name"] == ["Pancakes"]
        assert item["properties"]["cookTime"] == ["PT15M"]
        assert item["properties"]["image"] == ["https://r.example/p.jpg"]
        author = item["properties"]["author"][0]
        assert author["properties"]["name"] == ["Sam"]

    def test_nested_typeof_not_reported_as_top_level(self):
        assert len(extract_structured(RDFA).rdfa) == 1


class TestFindType:
    def test_searches_every_syntax(self):
        page = PRODUCT_PAGE.replace("</body>", MICRODATA + "</body>")
        data = extract_structured(page)
        products = data.find_type("Product")
        assert len(products) == 2
        names = sorted(str(p.get("name")) for p in products)
        assert names == ["Trail Runner Pro", "Widget"]

    def test_finds_nested_json_ld_nodes(self):
        data = extract_structured(PRODUCT_PAGE)
        offers = data.find_type("Offer")
        assert offers and offers[0]["priceCurrency"] == "USD"

    def test_type_match_is_case_insensitive_and_suffix_based(self):
        data = extract_structured(PRODUCT_PAGE)
        assert data.find_type("product")
        assert not data.find_type("Recipe")


class TestEdgeCases:
    def test_empty_and_bytes_input(self):
        assert not extract_structured("")
        assert not extract_structured("   ")
        assert extract_structured(PRODUCT_PAGE.encode()).json_ld

    def test_to_dict_shape(self):
        d = extract_structured(PRODUCT_PAGE).to_dict()
        assert set(d) == {"json_ld", "microdata", "rdfa", "opengraph", "twitter", "meta", "links"}

    def test_dataclass_default_is_falsy(self):
        assert not StructuredData()

    def test_parsed_response_exposes_structured_data(self):
        resp = Response(
            url="https://shop.example/p/1",
            status=200,
            headers={},
            body=PRODUCT_PAGE.encode(),
            request=Request(url="https://shop.example/p/1"),
        )
        page = ParsedResponse(resp)
        assert page.json_ld[0]["@type"] == "Product"
        assert page.opengraph["og:title"] == "Trail Runner Pro"
        assert page.structured is page.structured  # cached
        assert page.structured.links["canonical"][0]["href"] == "https://shop.example/p/1"
