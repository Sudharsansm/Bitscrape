# Plugins

`bitscrape.plugins` is a generic hook/event system: plugins register
callbacks against named lifecycle events, and `PluginManager` fires them at
the right point during a crawl. This is the extension point for things
like authentication helpers and cloud-storage connectors.

**Deliberately not included** as built-in plugins: CAPTCHA-solving
integrations or "anti-bot" extensions. Those are tooling whose specific
purpose is defeating a site's anti-bot protections against its wishes —
that's a different category of thing from a generic extension point, and
isn't something this project ships regardless of how it's packaged.

## Lifecycle events

| Event | Fired when | Kwargs passed |
|---|---|---|
| `spider_opened` | Once, before crawling starts | `spider` |
| `spider_closed` | Once, after crawling finishes | `spider`, `reason` (`"finished"`/`"cancelled"`) |
| `request_scheduled` | Every time a request is enqueued (seeds and follow-ups) | `request`, `spider` |
| `response_received` | Every successful fetch, before parsing | `request`, `response`, `spider` |
| `item_scraped` | Every item that passes noindex checks and pipelines | `item`, `spider` |
| `item_dropped` | Every item a pipeline drops (`DropItem`) | `item`, `exception`, `spider` |
| `error` | Every download or parse error | `request`, `exception`, `spider` |

A callback raising an exception is logged (with full traceback) and does
**not** stop other callbacks or the crawl — a broken plugin shouldn't take
down a whole run. Verified by test: a rule/callback that raises is caught,
logged, and subsequent callbacks still run.

## Registering callbacks directly

```python
from bitscrape.plugins import PluginManager

pm = PluginManager()
pm.on("item_scraped", lambda item, spider: print(f"Got: {item}"))

async def on_error(request, exception, spider):
    print(f"Failed: {request.url} -- {exception}")

pm.on("error", on_error)
```

Both sync and async callbacks work — `PluginManager.fire()` awaits the
result only if it's awaitable.

## The `BasePlugin` convenience class

For more than one or two hooks, subclass `BasePlugin` and override only
what you need — unused hooks stay as harmless no-ops:

```python
from bitscrape.plugins import BasePlugin, PluginManager

class LoggingPlugin(BasePlugin):
    async def spider_opened(self, spider):
        print(f"Starting {spider.name}")

    async def item_scraped(self, item, spider):
        print(f"Scraped: {item}")

    async def spider_closed(self, spider, reason):
        print(f"Finished ({reason})")

pm = PluginManager()
pm.register_plugin(LoggingPlugin())
```

`register_plugin()` wires up every event method the plugin actually
overrides — you don't need to call `.on()` for each one manually.

## Using plugins with `build_engine()` / `bitscrape.run()`

```python
import bitscrape
from bitscrape.plugins import PluginManager

pm = PluginManager()
pm.register_plugin(MyPlugin())

engine = await bitscrape.build_engine(MySpider(), bitscrape.Settings(), plugin_manager=pm)
stats = await engine.run()
```

`monitoring_enabled=True` and `metrics_enabled=True` on `Settings` register
their own internal plugins onto whatever `PluginManager` you pass (or a
fresh one if you don't) — see [monitoring/](../monitoring/index.md).

## Bundled example plugins

### `BearerTokenAuthPlugin`

Injects an `Authorization: Bearer <token>` header into every scheduled
request matching a given domain — a legitimate use of the plugin
architecture: "attach credentials I already have to requests to a site I'm
authorized to access."

```python
from bitscrape.plugins import BearerTokenAuthPlugin

pm.register_plugin(BearerTokenAuthPlugin(domain="api.example.com", token="secret123"))
```

Acts on the `request_scheduled` hook, mutating `request.headers` in place
before the request is enqueued.

### `StorageConnectorPlugin`

Streams every scraped item into any `BaseStorageBackend` as it's scraped,
opening the backend on `spider_opened` and closing it on `spider_closed`:

```python
from bitscrape.storage.backends import SQLiteStorageBackend
from bitscrape.plugins import StorageConnectorPlugin

pm.register_plugin(StorageConnectorPlugin(SQLiteStorageBackend("out.db")))
```

See [storage/](../storage/index.md) for backend options.

## Writing your own plugin: a checklist

1. Subclass `BasePlugin`, override only the hooks you need.
2. Keep hook methods fast and non-blocking where possible — they run
   inline in the crawl loop (`response_received` and `item_scraped` fire
   once per response/item).
3. Don't assume hook ordering across *different* plugins registered on the
   same event — if order matters, register them in the order you want them
   called (they run in registration order for a given event).
4. If your plugin needs cleanup, do it in `spider_closed`, not by relying
   on garbage collection.
5. Test it: register your plugin, run a real crawl against a real local
   server, and assert on what your plugin actually did (see
   `tests/test_plugin_engine_integration.py` for the pattern this project
   uses).

## See also

- [architecture/](../architecture/index.md) — where plugin hooks fire relative to the rest of the crawl loop.
- [monitoring/](../monitoring/index.md) — `StatsMonitor.as_plugin()`, the built-in metrics plugin.
- [api/index.md#plugins-bitscrapeplugins](../api/index.md#plugins-bitscrapeplugins) — full signatures.
