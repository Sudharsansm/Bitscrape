# Security

This page describes Bitscrape's security-relevant behavior and posture.
For reporting a vulnerability, see the root [`SECURITY.md`](../../SECURITY.md).

## What this project deliberately does not build

Bitscrape will not implement, and has repeatedly declined requests to add:

- **CAPTCHA-solving integrations.**
- **Bot-detection evasion**: browser/TLS fingerprint spoofing, canvas/WebGL
  spoofing, mouse/keystroke simulation designed to mimic human behavior.
- **"Anti-bot" plugin categories** of any kind.

The reasoning is consistent, not case-by-case: this is tooling whose
specific purpose is defeating a site's anti-bot protections against its
wishes. That's a different category of thing from legitimate crawler
infrastructure (rate limiting, retries, distributed coordination), and
isn't something this project ships regardless of how a request is framed
(a "defensive testing" framing, a fictional/simulation framing, or
incremental small requests that add up to the same capability).

## What this project does to behave well by default

- **robots.txt compliance is on by default**
  (`Settings.robotstxt_obey = True`) -- `Disallow`, `Crawl-delay`, and
  `Sitemap` directives are all honored, and a fetch failure fails *safe*
  (blocks the domain until the rules can be confirmed) rather than *open*
  (silently allowing everything).
- **Meta-robots compliance is on by default**
  (`Settings.respect_meta_robots = True`) -- page-level `noindex`/`nofollow`
  directives are honored even when robots.txt doesn't cover them.
- **A realistic, identifiable default User-Agent** (`BitscrapeBot/0.1 ...`)
  -- the project does not encourage impersonating a real browser's UA to
  evade detection; `UserAgentMiddleware`'s rotation is for load
  distribution across a small pool of legitimate strings, not evasion.
- **No credential vault or secrets baked into the framework.** Auth headers
  (see `BearerTokenAuthPlugin`) and connection strings (Redis, Postgres,
  S3) are passed explicitly by you, typically from environment variables
  or a secrets manager you control -- the framework itself doesn't persist
  or transmit credentials anywhere beyond the requests you configure it to
  make.

## Handling credentials safely

- Prefer environment variables (`BITSCRAPE_REDIS_URL`,
  `BITSCRAPE_DATABASE_URL`, etc.) or your platform's secrets manager over
  hardcoding connection strings/tokens in spider code.
- In Kubernetes, use a `Secret` (referenced via `secretKeyRef`, as shown in
  `deploy/k8s/deployment.yaml`), not a `ConfigMap`, for anything sensitive.
- `BearerTokenAuthPlugin` and similar auth helpers assume you already have
  legitimate credentials for the target site -- this project provides the
  plumbing to attach them to requests, not a way to obtain credentials you
  don't have.

## Dependency and supply-chain practices

- Exact dev-tool versions are pinned in `pyproject.toml`
  (`ruff==0.15.22`, `mypy==2.3.0`, `pytest==9.1.1`, `pytest-asyncio==1.4.0`,
  `moto[s3]==5.2.2`, `pyyaml==6.0.3`) for reproducible builds -- this was
  added after discovering that an unpinned `ruff` could silently resolve to
  a stricter newer version, which is a reproducibility concern more than a
  security one, but the same pinning discipline applies to anything
  security-relevant you add.
- Runtime dependencies are declared as version ranges (`>=`) in
  `pyproject.toml`'s optional extras -- review and pin them yourself for
  production deployments if you need fully reproducible/audited builds.

## Container security

- `deploy/Dockerfile` runs as a non-root user (`bitscrape`, uid 1000) in
  the runtime stage.
- The multi-stage build means build toolchains (compilers, `-dev` headers)
  don't end up in the final image, reducing attack surface.
- See [deployment/](../deployment/index.md) for the honest disclosure on
  what's syntax-validated vs. live-cluster-tested for the Kubernetes
  manifests.

## Data you scrape is your responsibility

Bitscrape is a tool for fetching and parsing publicly-reachable HTTP
resources according to the configuration you give it (respecting
robots.txt/meta-robots by default, as above). It has no awareness of the
legal status of any particular site's terms of service, data protection
regulations (GDPR, CCPA, etc.) applicable to what you scrape and store, or
licensing on the content itself. That determination is yours to make for
each target and each use of the resulting data.

## Reporting a vulnerability

See [`SECURITY.md`](../../SECURITY.md).
