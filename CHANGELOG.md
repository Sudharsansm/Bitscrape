# Changelog

All notable changes to Bitscrape will be documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [0.1.0] - 2024-01-01

### Added
- Async HTTP downloader (aiohttp) with retry logic
- Spider base class with CSS/XPath selector support (selectolax + parsel)
- Scheduler with MemoryQueue and RedisQueue backends
- Fingerprint-based duplicate URL filter
- Item pipeline framework (Logging, Validation, Dedup, Postgres)
- Feed exporters: JSONL, JSON, CSV, XML
- Middleware system: UserAgent, Robots.txt, Cookies
- LangGraph state machine workflow orchestration
- CLI: crawl, startproject, genspider, list commands
- Single-import API: `import bitscrape`
- `bitscrape.run()` one-liner helper
- FormRequest for POST/form submissions
- Playwright support for JavaScript-rendered pages
- Pydantic v2 type-safe models throughout
- PEP 561 typed package marker
