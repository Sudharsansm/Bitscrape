# MCP server

`pip install "bitscrape[mcp]"` then `bitscrape mcp` (stdio, for Claude Desktop /
Cursor) or `bitscrape mcp --transport streamable-http --auth-token $TOKEN`.

## Tools

| Tool | What it does |
|---|---|
| `fetch` | URL → clean Markdown (main content, hidden/injected text stripped); `css_selector` converts only matches; indexes for `search` |
| `extract` | CSS-select only what you need (text or an attribute) -- far fewer tokens than a page |
| `list_links`, `bulk_fetch` | links on a page; up to 25 URLs concurrently |
| `extract_structured` | JSON-LD, Microdata, RDFa, OpenGraph, meta |
| `search` | BM25 (or hybrid) search over everything fetched so far |
| `screenshot` | base64 PNG |
| `session_open` / `session_fetch` / `session_close` / `session_list` | a persistent browser: log in once, keep cookies across calls |
| `session_save_state` / `session_load_state` | save/restore a login by name |
| `network_search` | background API/XHR responses and HTTP errors a session's pages made, searchable, with truncated bodies |

Browser sessions block ads by default, record requests, and attach to a remote
browser with `--cdp-url`.

## Safety defaults

- Public `http(s)` only: loopback, private, link-local (cloud metadata) and
  reserved targets are refused (`--allow-private-networks` to permit). The check
  covers the given URL and the final URL after redirects -- a guard rail, not a sandbox.
- `cdp_url` is a server option, never an agent argument.
- Saved states live in `~/.bitscrape/mcp-state` (dir 0700, files 0600); names are validated.
- HTTP transport: optional bearer token (constant-time compare); binding a
  non-loopback address without a token is refused.
- Server instructions tell the model to treat page content as untrusted.
- There is no fetch mode that evades bot protection, and no CAPTCHA solving.

```json
{"mcpServers": {"bitscrape": {"command": "bitscrape", "args": ["mcp"]}}}
```
