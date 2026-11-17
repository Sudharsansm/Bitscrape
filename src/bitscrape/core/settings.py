"""
Global settings loaded from env vars or .env files.
All env vars are prefixed with BITSCRAPE_.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # --- Concurrency ---------------------------------------------------------
    concurrent_requests: int = Field(default=16, ge=1, le=1024)
    # Keep every successfully-piped item in memory for the crawl's duration,
    # exposed as CrawlStats.items (an ItemList: .to_json()/.to_csv()/
    # .to_parquet()/... for all 17 built-in formats). Off by default -- a
    # large crawl shouldn't silently buffer millions of items; turn this on
    # for small/medium crawls or interactive use, and prefer a file exporter
    # (get_exporter() / the CLI's --format) for anything bigger.
    collect_items: bool = False
    concurrent_requests_per_domain: int = Field(default=4, ge=1)
    download_delay: float = Field(default=0.0, ge=0.0)  # seconds between requests (per domain)

    # --- Downloader ----------------------------------------------------------
    download_timeout: float = Field(default=30.0, ge=1.0)
    retry_http_codes: list[int] = Field(default_factory=lambda: [500, 502, 503, 504, 429])
    user_agent: str = "BitscrapeBot/0.1 (+https://github.com/yourorg/bitscrape)"
    # Ordinary, non-impersonating HTTP hygiene: sensible header values every
    # normal client (browser or not) sends, merged under an explicit
    # User-Agent (which stays separate/overridable via `user_agent` above and
    # per-session/per-request `headers=`). This is NOT browser fingerprint
    # impersonation -- it's one fixed, honest default, not a per-browser
    # profile chosen to make the request look like it came from somewhere
    # else. Override or clear per session/request as needed.
    default_headers: dict[str, str] = Field(
        default_factory=lambda: {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    follow_redirects: bool = True
    max_redirect_count: int = 10

    # --- Scheduler -----------------------------------------------------------
    scheduler_use_redis: bool = False
    redis_url: str = "redis://localhost:6379/0"
    dupefilter_enabled: bool = True
    max_depth: int | None = None  # None = unlimited
    queue_max_size: int = Field(
        default=0, ge=0
    )  # 0 = unbounded (previous behaviour); >0 caps MemoryQueue for backpressure

    # --- Playwright ----------------------------------------------------------
    playwright_headless: bool = True
    playwright_browser: str = "chromium"
    playwright_pool_size: int = 2
    playwright_pool_enabled: bool = False

    # --- Storage -------------------------------------------------------------
    database_url: str | None = None  # asyncpg DSN
    supabase_url: str | None = None
    supabase_key: str | None = None

    # --- Logging / observability --------------------------------------------
    log_level: str = "INFO"
    stats_dump_interval: float = 60.0  # seconds

    # --- Exports -------------------------------------------------------------
    feed_uri: str | None = None  # e.g. "data.jsonl" or "s3://bucket/key"
    feed_format: str = "jsonl"  # json | jsonl | csv | xml

    # --- Robots.txt ----------------------------------------------------------
    robotstxt_obey: bool = True
    # User-Agent used both to fetch robots.txt and to pick the matching
    # User-agent group inside it (default: the crawl's own ``user_agent``).
    robots_user_agent: str | None = None
    # Put the full Request-rate directive in request.meta["request_rate"] as
    # well as folding its implied spacing into meta["crawl_delay"].
    robots_respect_request_rate: bool = True
    # Re-check a domain's robots.txt after this many seconds (it can change
    # during a long crawl); a failed refresh keeps the last known-good rules.
    robots_cache_ttl: float = Field(default=3600.0, ge=0.0)
    # How long a *failed* fetch is remembered before being retried, so a
    # domain that's down isn't refetched on every single request to it.
    robots_negative_cache_ttl: float = Field(default=30.0, ge=0.0)
    # LRU cap on how many domains' robots.txt are cached at once.
    robots_max_cache_domains: int = Field(default=10_000, ge=1)
    # A robots.txt body larger than this is truncated before parsing
    # (500 KiB is the informal convention several crawlers follow).
    robots_max_content_bytes: int = Field(default=500 * 1024, ge=1)
    # Timeout for fetching robots.txt itself.
    robots_timeout: float = Field(default=10.0, gt=0.0)

    # --- Page-level indexing directives (meta robots / X-Robots-Tag) --------
    # Mirrors what Googlebot/ClaudeBot/PerplexityBot honour beyond robots.txt:
    # a page can say "you may fetch me, but don't index me" (noindex) or
    # "don't pass link-equity / don't follow links found here" (nofollow).
    respect_meta_robots: bool = True

    # --- Conditional GET (ETag / Last-Modified) ------------------------------
    # Avoids re-downloading unchanged pages on repeat crawls, same as
    # production search-engine crawlers do to reduce load on crawled sites.
    conditional_get_enabled: bool = True

    # --- Retry-After handling -------------------------------------------------
    # Honour a server's own requested backoff (RFC 9110 Retry-After) instead
    # of blindly using exponential backoff on 429/503.
    respect_retry_after: bool = True
    max_retry_after_seconds: float = Field(default=120.0, ge=0.0)

    # --- Distributed rate limiting -------------------------------------------
    # A Redis-backed per-domain lease so multiple worker processes sharing
    # the same Redis don't independently hammer the same domain -- each
    # worker still crawls concurrently, but requests to any one domain are
    # spaced out cluster-wide, not just within a single process.
    distributed_throttle_enabled: bool = False

    # --- Auto-throttle --------------------------------------------------------
    # Adaptive, latency-based per-domain delay: back off when responses get
    # slow, speed back up as they recover. Local to this process (doesn't
    # need Redis) -- combine with distributed_throttle_enabled for
    # multi-worker deployments.
    autothrottle_enabled: bool = False
    autothrottle_start_delay: float = Field(default=1.0, ge=0.0)
    autothrottle_max_delay: float = Field(default=60.0, ge=0.0)
    autothrottle_target_concurrency: float = Field(default=2.0, gt=0.0)
    # Reacting to trouble (all only matter when autothrottle_enabled):
    #   * a response whose status is in ``autothrottle_backoff_codes``, one the
    #     blocked-request detector flags, or (if ``..._on_errors``) a timeout /
    #     connection error, multiplies the domain's delay by
    #     ``autothrottle_backoff_factor``;
    #   * if that response carries ``Retry-After``, the whole domain pauses for
    #     that long instead (capped at ``max_retry_after_seconds``);
    #   * each healthy response closes ``autothrottle_recovery`` of the gap back
    #     to the delay the site can sustain (0.5 = halve it; 1.0 = jump there).
    # The delay never drops below ``download_delay`` / ``domain_delays`` /
    # robots.txt ``Crawl-delay``.
    autothrottle_block_backoff: bool = True
    autothrottle_backoff_factor: float = Field(default=2.0, ge=1.0)
    autothrottle_recovery: float = Field(default=0.5, gt=0.0, le=1.0)
    autothrottle_backoff_codes: list[int] = Field(default_factory=lambda: [429, 503])
    autothrottle_backoff_on_errors: bool = True

    # --- Session pooling ------------------------------------------------------
    session_pool_size: int = Field(default=1, ge=1)
    session_rotate_every: int = Field(default=0, ge=0)  # 0 = never auto-rotate

    # --- Proxy rotation (used by build_engine() in bitscrape.factory) --------
    proxies: list[str] = Field(default_factory=list)
    proxy_rotate: bool = True

    # --- Monitoring / metrics (used by build_engine()) ------------------------
    monitoring_enabled: bool = False
    monitoring_port: int = 8765
    metrics_enabled: bool = False
    metrics_port: int = 9100

    # ======================================================================
    # Added in 0.9 -- every setting below is opt-in; defaults preserve the
    # behaviour of earlier releases.
    # ======================================================================

    # --- Per-domain throttling overrides ------------------------------------
    # {"example.com": 2.0} -> minimum seconds between requests to that domain
    # (takes precedence over ``download_delay``); likewise for concurrency.
    domain_delays: dict[str, float] = Field(default_factory=dict)
    domain_concurrency: dict[str, int] = Field(default_factory=dict)

    # --- Pause / resume (checkpointing) ---------------------------------------
    # Directory holding the persisted crawl state. When set, Ctrl-C (or
    # ``engine.pause()``) saves pending requests + seen fingerprints, and the
    # next run with the same directory resumes where it left off.
    jobdir: str | None = None
    checkpoint_interval: float = Field(default=30.0, ge=0.0)  # 0 disables periodic saves

    # --- Dev mode: cache every response on disk -------------------------------
    dev_mode: bool = False
    dev_cache_dir: str = ".bitscrape/httpcache"
    dev_cache_ttl: float = Field(default=0.0, ge=0.0)  # seconds; 0 = never expires
    dev_cache_statuses: list[int] = Field(default_factory=lambda: [200])

    # --- Blocked-request detection & retry -------------------------------------
    blocked_detection_enabled: bool = False
    blocked_http_codes: list[int] = Field(default_factory=lambda: [403, 429])
    blocked_max_retries: int = Field(default=3, ge=0)
    blocked_retry_backoff: float = Field(default=2.0, ge=0.0)  # base seconds, doubled per attempt
    blocked_max_backoff: float = Field(default=30.0, ge=0.0)
    blocked_extra_markers: list[str] = Field(default_factory=list)

    # --- Proxy rotation (extended) ---------------------------------------------
    proxy_strategy: str = "random"  # random | round_robin | least_used | sticky
    proxy_file: str | None = None  # one proxy URL per line
    proxy_ban_threshold: int = Field(default=3, ge=1)  # consecutive failures before a cool-down
    proxy_ban_seconds: float = Field(default=300.0, ge=0.0)

    # --- HTTP layer: TLS/HTTP2 fingerprint impersonation, HTTP/3 ---------------
    impersonate: str | None = None  # e.g. "chrome", "chrome131", "safari18_0", "firefox135"
    http3: bool = False  # ask the impersonating fetcher to prefer HTTP/3
    verify_ssl: bool = True

    # --- Multi-session: named fetch sessions inside one spider -----------------
    # {"api": {"kind": "http", "headers": {...}},
    #  "rendered": {"kind": "browser", "wait_selector": ".price", "lazy": True,
    #               "default": False}, ...}
    # Each entry's keys are the constructor arguments of ``HttpSession`` /
    # ``BrowserSession`` plus ``kind``, ``lazy`` and ``default``. Sessions can
    # also be added in code via ``Spider.configure_sessions(manager)``.
    sessions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # How many times one request may be re-queued via
    # ``Spider.retry_blocked_request()`` (e.g. to move it to another session).
    blocked_escalation_max: int = Field(default=2, ge=0)

    # --- Browser engine ---------------------------------------------------------
    browser_driver: str = "playwright"  # only "playwright"; patched drivers (e.g. "patchright") raise NotImplementedError
    browser_cdp_url: str | None = None  # connect to a remote browser over CDP
    browser_executable_path: str | None = None
    browser_channel: str | None = None  # e.g. "chrome", "msedge"
    browser_args: list[str] = Field(default_factory=list)
    browser_wait_until: str = "networkidle"  # load | domcontentloaded | networkidle | commit
    browser_wait_selector: str | None = None
    browser_timeout: float = Field(default=30.0, ge=1.0)
    browser_user_data_dir: str | None = None  # persistent profile (keeps logins)
    browser_storage_state: str | None = None  # Playwright storage_state JSON file
    browser_locale: str | None = None
    browser_timezone: str | None = None
    browser_viewport: tuple[int, int] | None = None
    browser_capture_xhr: bool | str = False  # True = all XHR/fetch; str = URL regex
    browser_stealth: bool = False  # deliberately unsupported: setting it raises NotImplementedError (ROADMAP.md, "Explicit non-goals")

    # --- Ad / tracker / domain blocking ------------------------------------------
    block_ads: bool = False  # built-in ad & tracker domain list
    block_domains: list[str] = Field(default_factory=list)
    block_resource_types: list[str] = Field(default_factory=list)  # image, font, media, ...
    block_hosts_file: str | None = None  # hosts-format or one-domain-per-line list

    # --- DNS-over-HTTPS (privacy) ----------------------------------------------------
    # Resolve hostnames for the HTTP stack through a DoH server instead of the
    # system resolver (see bitscrape.dns). ``doh_url`` is a provider name
    # ("cloudflare" | "google" | "quad9") or an https:// URL; default Cloudflare.
    dns_over_https: bool = False
    doh_url: str | None = None
    # Deliberately unsupported (resolver locking / WebRTC suppression to hide
    # proxied traffic from sites): setting it raises NotImplementedError.
    dns_leak_protection: bool = False

    # --- Browser network recording / shadow DOM ----------------------------------------
    browser_record_requests: bool = False  # keep a searchable history of completed requests
    browser_max_recorded_requests: int = Field(default=1000, ge=1)
    browser_pierce_shadow: bool = False  # include open shadow roots in rendered HTML

    # --- Adaptive selectors ---------------------------------------------------------
    adaptive_db_path: str = ".bitscrape/adaptive.sqlite"

    model_config = SettingsConfigDict(
        env_prefix="BITSCRAPE_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @model_validator(mode="after")
    def _check_autothrottle_bounds(self) -> Settings:
        if (
            self.autothrottle_enabled
            and self.autothrottle_max_delay < self.autothrottle_start_delay
        ):
            raise ValueError(
                "autothrottle_max_delay "
                f"({self.autothrottle_max_delay}) can't be lower than "
                f"autothrottle_start_delay ({self.autothrottle_start_delay})"
            )
        return self
