"""
Global settings loaded from env vars or .env files.
All env vars are prefixed with BITSCRAPE_.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # --- Concurrency ---------------------------------------------------------
    concurrent_requests: int = Field(16, ge=1, le=1024)
    concurrent_requests_per_domain: int = Field(4, ge=1)
    download_delay: float = Field(0.0, ge=0.0)  # seconds between requests (per domain)

    # --- Downloader ----------------------------------------------------------
    download_timeout: float = Field(30.0, ge=1.0)
    retry_http_codes: list[int] = Field(default_factory=lambda: [500, 502, 503, 504, 429])
    user_agent: str = "BitscrapeBot/0.1 (+https://github.com/yourorg/bitscrape)"
    follow_redirects: bool = True
    max_redirect_count: int = 10

    # --- Scheduler -----------------------------------------------------------
    scheduler_use_redis: bool = False
    redis_url: str = "redis://localhost:6379/0"
    dupefilter_enabled: bool = True
    max_depth: int | None = None  # None = unlimited
    queue_max_size: int = Field(
        0, ge=0
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
    max_retry_after_seconds: float = Field(120.0, ge=0.0)

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
    autothrottle_start_delay: float = Field(1.0, ge=0.0)
    autothrottle_max_delay: float = Field(60.0, ge=0.0)
    autothrottle_target_concurrency: float = Field(2.0, gt=0.0)

    # --- Session pooling ------------------------------------------------------
    session_pool_size: int = Field(1, ge=1)
    session_rotate_every: int = Field(0, ge=0)  # 0 = never auto-rotate

    # --- Proxy rotation (used by build_engine() in bitscrape.factory) --------
    proxies: list[str] = Field(default_factory=list)
    proxy_rotate: bool = True

    # --- Monitoring / metrics (used by build_engine()) ------------------------
    monitoring_enabled: bool = False
    monitoring_port: int = 8765
    metrics_enabled: bool = False
    metrics_port: int = 9100

    model_config = SettingsConfigDict(
        env_prefix="BITSCRAPE_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )
