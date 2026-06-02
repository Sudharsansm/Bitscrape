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

    # --- Playwright ----------------------------------------------------------
    playwright_headless: bool = True
    playwright_browser: str = "chromium"
    playwright_pool_size: int = 2

    # --- Storage -------------------------------------------------------------
    database_url: str | None = None  # asyncpg DSN
    supabase_url: str | None = None
    supabase_key: str | None = None

    # --- Logging / observability --------------------------------------------
    log_level: str = "INFO"
    stats_dump_interval: float = 60.0  # seconds

    # --- Exports -------------------------------------------------------------
    feed_uri: str | None = None          # e.g. "data.jsonl" or "s3://bucket/key"
    feed_format: str = "jsonl"           # json | jsonl | csv | xml

    # --- Robots.txt ----------------------------------------------------------
    robotstxt_obey: bool = True

    model_config = SettingsConfigDict(
        env_prefix="BITSCRAPE_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )
