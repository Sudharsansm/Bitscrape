# Security Policy

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Instead, report privately to the maintainers (via your repository host's
private security advisory feature, e.g. GitHub's "Report a vulnerability"
under the Security tab, if enabled for this repository). Include:

- A description of the vulnerability and its potential impact.
- Steps to reproduce, or a minimal proof-of-concept if possible.
- The version/commit you tested against.

We'll acknowledge your report and work with you on a fix and disclosure
timeline before any public details are shared.

## Supported versions

This project follows the version in `pyproject.toml`. Security fixes are
applied to the latest released version; older versions are not
separately maintained unless stated otherwise at the time.

| Version | Supported |
|---|---|
| 0.7.x (latest) | Yes |
| < 0.7.0 | No -- please upgrade |

## What's in scope

- Vulnerabilities in this project's own code (`src/bitscrape/`).
- Vulnerabilities in how this project's defaults or documented usage could
  lead to unintended data exposure, credential leakage, or unsafe behavior.
- Vulnerabilities in the `deploy/` Docker/Kubernetes artifacts (e.g.
  running as root, unnecessary exposed ports, secrets handling).

## What's out of scope

- Vulnerabilities in third-party dependencies -- please report those
  upstream to the relevant project (`aiohttp`, `playwright`, `redis-py`,
  etc.), though we're happy to hear about them too if they materially
  affect how this project uses that dependency.
- The behavior of websites you scrape with this tool, or the legality of
  scraping any particular target -- see [`docs/security/`](docs/security/index.md)
  for the project's position on this ("data you scrape is your
  responsibility").
- Requests to add bot-detection evasion, CAPTCHA-solving, or fingerprint
  spoofing framed as "security research" -- these are declined as
  out-of-scope for this project regardless of framing; see
  [`docs/security/`](docs/security/index.md) and `ROADMAP.md`'s explicit
  non-goals.

## Our commitments

- We will not take legal action against good-faith security researchers
  who report vulnerabilities responsibly through the private channel
  above and give us reasonable time to address them before public
  disclosure.
- We will credit reporters (if desired) in the relevant `CHANGELOG.md`
  entry once a fix ships.

## General security posture

See [`docs/security/`](docs/security/index.md) for a fuller description of
this project's security-relevant defaults (robots.txt/meta-robots
compliance on by default, no bundled credential vault, non-root Docker
runtime user, pinned dev-tool versions for reproducible builds) and what
it deliberately does not build.
