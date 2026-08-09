"""
Bitscrape Canonicalization
==========================

Proper handling of canonical URLs, redirects, and duplicate content -- the
piece that keeps a large crawl from treating
``https://Example.com/page/?utm_source=x#top`` and
``http://example.com/page`` as two different URLs, or crawling the same
article twice because a redirect landed on an already-seen page.

Three pieces:
  1. ``canonicalize_url()`` -- normalizes a single URL (scheme/host case,
     default ports, query param sorting, tracking-param stripping, fragment
     removal, trailing-slash consistency).
  2. ``resolve_redirect_chain()`` -- given a sequence of hops (as your
     downloader would report them), returns the final canonical destination
     and detects redirect loops.
  3. ``ContentFingerprint`` / ``is_near_duplicate()`` -- SimHash-based near-
     duplicate detection for page bodies, so pages that are byte-different
     but substantively the same (tracking pixel changed, timestamp in
     footer, etc.) are recognized as duplicates. Exact-duplicate detection
     (identical bytes) is a trivial hash comparison and doesn't need this;
     SimHash is for the "almost identical" case exact hashing misses.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Common tracking / session parameters that don't change page identity.
# Deliberately conservative -- prefixes cover the utm_* family broadly,
# exact names cover the rest.
_TRACKING_PREFIXES = ("utm_", "mc_", "ga_")
_TRACKING_EXACT = frozenset(
    {
        "fbclid",
        "gclid",
        "msclkid",
        "ref",
        "referrer",
        "session_id",
        "sessionid",
        "sid",
        "_ga",
        "_gl",
        "igshid",
        "mkt_tok",
        "spm",
    }
)

_DEFAULT_PORTS = {"http": 80, "https": 443}


def canonicalize_url(
    url: str,
    strip_tracking_params: bool = True,
    strip_fragment: bool = True,
    strip_trailing_slash: bool = True,
    extra_tracking_params: frozenset[str] | None = None,
) -> str:
    """
    Normalizes a URL to a canonical form:
      - lowercase scheme and host
      - default ports removed (`:80` for http, `:443` for https)
      - query parameters sorted, with tracking parameters stripped
      - fragment removed (fragments don't identify different server content)
      - trailing slash removed from the path (except for the bare root `/`)

    Two URLs that mean "the same page" should canonicalize to the same
    string; this is deliberately conservative (won't strip parameters that
    might be meaningful, like `?page=2`) rather than aggressive.
    """
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower()
    raw_netloc = parsed.netloc

    if "@" in raw_netloc:
        userinfo, _, hostport = raw_netloc.rpartition("@")
        host_prefix = f"{userinfo}@"
        netloc = hostport.lower()
    else:
        host_prefix = ""
        netloc = raw_netloc.lower()

    if ":" in netloc:
        host, _, port_str = netloc.partition(":")
        try:
            port = int(port_str)
            if _DEFAULT_PORTS.get(scheme) == port:
                netloc = host
        except ValueError:
            pass  # malformed port, leave as-is

    netloc = host_prefix + netloc

    path = parsed.path or "/"
    if strip_trailing_slash and len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
        if not path:
            path = "/"

    tracking = _TRACKING_EXACT | (extra_tracking_params or frozenset())
    if strip_tracking_params:
        query_pairs = [
            (k, v)
            for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if not (k.lower() in tracking or k.lower().startswith(_TRACKING_PREFIXES))
        ]
    else:
        query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    query_pairs.sort()
    query = urlencode(query_pairs)

    fragment = "" if strip_fragment else parsed.fragment

    return urlunparse((scheme, netloc, path, parsed.params, query, fragment))


class RedirectLoopError(Exception):
    """Raised when a redirect chain revisits a URL it already visited."""


def resolve_redirect_chain(hops: list[str], max_hops: int = 20) -> str:
    """
    Given an ordered list of URLs a request was redirected through
    (``hops[0]`` is the original request, ``hops[-1]`` is where the server
    stopped redirecting), returns the canonical final destination.

    Raises ``RedirectLoopError`` if a URL (after canonicalization) appears
    twice in the chain, and ``ValueError`` if the chain exceeds ``max_hops``
    -- both real failure modes a crawler needs to detect rather than loop
    or follow forever.
    """
    if not hops:
        raise ValueError("Empty redirect chain")
    if len(hops) > max_hops:
        raise ValueError(f"Redirect chain exceeds max_hops={max_hops} ({len(hops)} hops)")

    seen: set[str] = set()
    for hop in hops:
        canon = canonicalize_url(hop)
        if canon in seen:
            raise RedirectLoopError(f"Redirect loop detected at {hop}")
        seen.add(canon)

    return canonicalize_url(hops[-1])


# ---------------------------------------------------------------------------
# Near-duplicate content detection (SimHash)
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _shingles(tokens: list[str], size: int = 4) -> set[str]:
    if len(tokens) < size:
        return {" ".join(tokens)} if tokens else set()
    return {" ".join(tokens[i : i + size]) for i in range(len(tokens) - size + 1)}


@dataclass(frozen=True)
class ContentFingerprint:
    """A 64-bit SimHash fingerprint of page text, plus the hex string form
    for storage/comparison. Two fingerprints with a small Hamming distance
    (a handful of differing bits out of 64) indicate near-duplicate content
    -- unlike an exact hash, this survives small edits (a changed date, an
    extra tracking pixel, minor whitespace differences)."""

    bits: int

    @property
    def hex(self) -> str:
        return f"{self.bits:016x}"

    def hamming_distance(self, other: ContentFingerprint) -> int:
        return (self.bits ^ other.bits).bit_count()


def compute_fingerprint(text: str, shingle_size: int = 4) -> ContentFingerprint:
    """
    Computes a 64-bit SimHash over 4-word shingles of the text. Standard
    SimHash construction: hash each shingle to 64 bits, then for each bit
    position sum +1/-1 across all shingle hashes weighted by shingle
    frequency, and set the output bit based on the sign of the sum.
    """
    tokens = _tokenize(text)
    shingles = _shingles(tokens, size=shingle_size)
    if not shingles:
        return ContentFingerprint(bits=0)

    bit_weights = [0] * 64
    for shingle in shingles:
        h = int.from_bytes(hashlib.blake2b(shingle.encode(), digest_size=8).digest(), "big")
        for i in range(64):
            bit_weights[i] += 1 if (h >> i) & 1 else -1

    result = 0
    for i in range(64):
        if bit_weights[i] > 0:
            result |= 1 << i
    return ContentFingerprint(bits=result)


def is_near_duplicate(
    text_a: str, text_b: str, max_hamming_distance: int = 3, shingle_size: int = 4
) -> bool:
    """
    True if two page texts are near-duplicates: their SimHash fingerprints
    differ in at most ``max_hamming_distance`` of 64 bits. The default (3)
    is a common, reasonably conservative threshold in SimHash literature
    for "substantively the same content" -- tune tighter (lower) for
    stricter matching, looser (higher) to catch more paraphrased/templated
    duplicates at the cost of more false positives.
    """
    fp_a = compute_fingerprint(text_a, shingle_size=shingle_size)
    fp_b = compute_fingerprint(text_b, shingle_size=shingle_size)
    return fp_a.hamming_distance(fp_b) <= max_hamming_distance
