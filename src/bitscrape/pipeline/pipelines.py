"""
Bitscrape Item Pipelines
========================
Items yielded by a spider pass through an ordered list of pipeline components.
Each component is an async class with:
  - ``open_spider(spider)``  — called before crawl
  - ``process_item(item, spider)``  — called for every item; return item or raise DropItem
  - ``close_spider(spider)``  — called after crawl

Built-in pipelines:
  - LoggingPipeline   – debug-log every item
  - ValidationPipeline – validate Pydantic items (drop invalid)
  - DedupPipeline     – drop duplicate items (by configurable key function)
  - PostgresPipeline  – async upsert to PostgreSQL via asyncpg
"""

from __future__ import annotations

import hashlib
import json
import logging
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sentinel exception
# ---------------------------------------------------------------------------


class DropItem(Exception):
    """Raise inside process_item to silently discard the item."""


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class BasePipeline(ABC):
    async def open_spider(self, spider: Any) -> None:  # noqa: B027
        """Called once before the spider starts."""

    @abstractmethod
    async def process_item(self, item: Any, spider: Any) -> Any:
        """Process one item. Return it to continue, raise DropItem to discard."""

    async def close_spider(self, spider: Any) -> None:  # noqa: B027
        """Called once after the spider finishes."""


# ---------------------------------------------------------------------------
# Built-in pipelines
# ---------------------------------------------------------------------------


class LoggingPipeline(BasePipeline):
    """Log every scraped item at DEBUG level."""

    async def process_item(self, item: Any, spider: Any) -> Any:
        logger.debug("[%s] item: %s", spider.name, item)
        return item


class ValidationPipeline(BasePipeline):
    """
    Drop items that fail Pydantic validation.
    Accepts dicts as well — tries to coerce to the registered Pydantic models.
    """

    def __init__(self, model: type[BaseModel] | None = None) -> None:
        self._model = model

    async def process_item(self, item: Any, spider: Any) -> Any:
        if self._model and isinstance(item, dict):
            try:
                item = self._model(**item)
            except ValidationError as exc:
                raise DropItem(f"Validation failed: {exc}") from exc
        if isinstance(item, BaseModel):
            try:
                item.model_validate(item.model_dump())
            except ValidationError as exc:
                raise DropItem(f"Pydantic validation failed: {exc}") from exc
        return item


class DedupPipeline(BasePipeline):
    """
    Drop items whose fingerprint was already seen.
    Default fingerprint = SHA-256 of sorted JSON dump.
    """

    def __init__(self, key_fn: Any = None) -> None:
        self._seen: set[str] = set()
        self._key_fn = key_fn or self._default_key

    @staticmethod
    def _default_key(item: Any) -> str:
        data = item.model_dump() if isinstance(item, BaseModel) else item
        serialised = json.dumps(data, sort_keys=True, default=str)
        return hashlib.sha256(serialised.encode()).hexdigest()

    async def process_item(self, item: Any, spider: Any) -> Any:
        key = self._key_fn(item)
        if key in self._seen:
            raise DropItem(f"Duplicate item: {key[:16]}…")
        self._seen.add(key)
        return item


class PostgresPipeline(BasePipeline):
    """
    Upsert items into a PostgreSQL table via asyncpg.
    Expects items to be Pydantic models.
    Configuration via spider.settings.database_url.
    """

    def __init__(self, table: str, conflict_cols: list[str] | None = None) -> None:
        self._table = table
        self._conflict_cols = conflict_cols or []
        self._conn: Any = None

    async def open_spider(self, spider: Any) -> None:
        db_url = spider.settings.database_url
        if not db_url:
            logger.warning("PostgresPipeline: no database_url configured, skipping")
            return
        try:
            import asyncpg

            self._conn = await asyncpg.connect(db_url)
            logger.info("PostgresPipeline connected to %s", db_url)
        except Exception as exc:
            logger.error("PostgresPipeline connection failed: %s", exc)

    async def process_item(self, item: Any, spider: Any) -> Any:
        if not self._conn:
            return item
        data = item.model_dump() if isinstance(item, BaseModel) else item
        cols = list(data.keys())
        vals = list(data.values())
        placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
        col_str = ", ".join(f'"{c}"' for c in cols)
        query = f'INSERT INTO "{self._table}" ({col_str}) VALUES ({placeholders})'
        if self._conflict_cols:
            conflict = ", ".join(f'"{c}"' for c in self._conflict_cols)
            updates = ", ".join(
                f'"{c}" = EXCLUDED."{c}"' for c in cols if c not in self._conflict_cols
            )
            query += f" ON CONFLICT ({conflict}) DO UPDATE SET {updates}"
        await self._conn.execute(query, *vals)
        return item

    async def close_spider(self, spider: Any) -> None:
        if self._conn:
            await self._conn.close()


# ---------------------------------------------------------------------------
# Pipeline manager
# ---------------------------------------------------------------------------


class PipelineManager:
    """
    Runs items through an ordered list of pipelines.
    Catches DropItem and counts dropped items.
    """

    def __init__(self, pipelines: list[BasePipeline]) -> None:
        self._pipelines = pipelines
        self.dropped = 0
        self.processed = 0

    async def open_spider(self, spider: Any) -> None:
        for p in self._pipelines:
            await p.open_spider(spider)

    async def process_item(self, item: Any, spider: Any) -> Any | None:
        for p in self._pipelines:
            try:
                item = await p.process_item(item, spider)
            except DropItem as exc:
                self.dropped += 1
                logger.debug("Dropped item: %s", exc)
                return None
        self.processed += 1
        return item

    async def close_spider(self, spider: Any) -> None:
        for p in self._pipelines:
            await p.close_spider(spider)
