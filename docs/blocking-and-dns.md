# Ad / tracker blocking and DNS-over-HTTPS

## Blocking (browser fetches)

```python
AsyncDynamicSession(block_ads=True, blocked_domains=["tracker.example"], allow_domains=["cdn.shop.example"])
Settings(block_ads=True, block_domains=[...], block_hosts_file="my-hosts.txt", block_resource_types=["image", "font"])
```

`block_ads=True` uses the bundled core list: **~3,900 ad/tracker domains**
(`src/bitscrape/data/ad_domains.txt`) -- the domains listed by *both* of two
independent MIT-licensed community lists (StevenBlack/hosts, anudeepND/blacklist),
plus a curated apex set. Subdomains of every entry match. Matching is a set
lookup over the host's dotted suffixes, so a list of 100k domains costs the same
per request as a list of 100. `allow_domains` always wins (false-positive escape
hatch). `DomainBlocker.blocked_count` counts refusals.

Refresh or widen the list:

```bash
bitscrape blocklist update                      # union of both sources (~100k domains), saved to ~/.cache/bitscrape/ad_domains.txt
bitscrape blocklist update --mode intersection  # high-confidence core only
bitscrape blocklist info
```

`update_blocklist()` is the same from Python; `BITSCRAPE_BLOCKLIST=/path` points
at a different file. This is a performance/noise feature -- it has nothing to do
with how a target site sees you.

## DNS-over-HTTPS

```python
Settings(dns_over_https=True, doh_url="cloudflare")   # or "google", "quad9", "https://..."
AsyncFetcherSession(dns_over_https=True)
HttpSession(dns_over_https="google")
```

Hostnames are resolved through a DoH server (JSON API) instead of the system
resolver, so local DNS queries aren't sent in clear text. Named providers are
addressed by IP, so locating the resolver needs no DNS lookup. Answers are
cached for their TTL; concurrent lookups share one query; IP literals and
`localhost` never hit the network; a failed lookup raises rather than silently
falling back (`DoHResolver(fallback=True)` opts in).

Scope: the aiohttp HTTP stack (`protocol="http1"`; the httpx HTTP/2 backend
raises if you combine it). Through a proxy it is the *proxy host* that is
resolved locally. The browser resolves names itself. `dns_leak_protection`
(resolver locking / WebRTC suppression) is not supported -- see ROADMAP.md.
