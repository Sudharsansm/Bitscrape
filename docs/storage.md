# Storage

Bitscrape can store scraped data in PostgreSQL (including Supabase) via
the `PostgresPipeline`, or export to files via Feed Exporters.

---

## PostgreSQL via asyncpg

### Install

```bash
pip install "bitscrape[postgres]"
```

### Configure

Set your database URL:

```env
BITSCRAPE_DATABASE_URL=postgresql://user:password@localhost:5432/mydb
```

Or in code:

```python
settings = bitscrape.Settings(
    database_url="postgresql://user:password@localhost:5432/mydb"
)
```

### Create your table

```sql
CREATE TABLE products (
    id        SERIAL PRIMARY KEY,
    title     TEXT NOT NULL,
    price     NUMERIC(10, 2),
    url       TEXT UNIQUE,
    in_stock  BOOLEAN DEFAULT TRUE,
    scraped_at TIMESTAMPTZ DEFAULT NOW()
);
```

### Use PostgresPipeline

```python
from bitscrape import PostgresPipeline

pipelines = [
    PostgresPipeline(
        table="products",
        conflict_cols=["url"],    # upsert on url conflict
    ),
]

bitscrape.run(MySpider, pipelines=pipelines)
```

The pipeline:
1. Connects on `open_spider`
2. Inserts each item as a row
3. Uses `ON CONFLICT ... DO UPDATE` if `conflict_cols` is set
4. Closes the connection on `close_spider`

---

## Supabase

Supabase is a managed PostgreSQL platform with extra features (real-time,
auth, REST API, storage). Bitscrape connects to it the same way as any
PostgreSQL database.

### Get your connection string

1. Go to your Supabase project dashboard
2. **Settings** → **Database** → **Connection string**
3. Copy the **URI** format:

```
postgresql://postgres:[PASSWORD]@db.[PROJECT-REF].supabase.co:5432/postgres
```

### Configure

```env
BITSCRAPE_DATABASE_URL=postgresql://postgres:mypassword@db.abcdefgh.supabase.co:5432/postgres
```

### Enable connection pooling (recommended for many workers)

Use the **pooler** connection string from Supabase:

```
postgresql://postgres.[PROJECT-REF]:[PASSWORD]@aws-0-us-east-1.pooler.supabase.com:6543/postgres
```

---

## File Storage (Supabase Storage / S3)

For media files (images, PDFs, screenshots), write a custom pipeline:

```python
import aiohttp
import bitscrape

class SupabaseStoragePipeline(bitscrape.BasePipeline):
    """Upload scraped images to Supabase Storage."""

    def __init__(self, bucket: str, supabase_url: str, supabase_key: str):
        self._bucket = bucket
        self._url = f"{supabase_url}/storage/v1/object/{bucket}"
        self._headers = {
            "Authorization": f"Bearer {supabase_key}",
            "Content-Type": "application/octet-stream",
        }
        self._session = None

    async def open_spider(self, spider):
        self._session = aiohttp.ClientSession()

    async def process_item(self, item, spider):
        image_url = (
            item.get("image_url")
            if isinstance(item, dict)
            else getattr(item, "image_url", None)
        )
        if image_url:
            async with self._session.get(image_url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    filename = image_url.split("/")[-1]
                    upload_url = f"{self._url}/{filename}"
                    async with self._session.post(upload_url,
                                                  data=data,
                                                  headers=self._headers) as r:
                        if r.status in (200, 201):
                            public_url = f"{self._url.replace('/object/', '/object/public/')}/{filename}"
                            if isinstance(item, dict):
                                item["image_storage_url"] = public_url
        return item

    async def close_spider(self, spider):
        if self._session:
            await self._session.close()
```

---

## Multiple Storage Targets

You can combine file export and database storage by using both an exporter
and a PostgresPipeline:

```python
import bitscrape
from bitscrape import PostgresPipeline, ValidationPipeline
from bitscrape.exporters.feed import get_exporter

bitscrape.run(
    MySpider,
    output="backup.jsonl",             # also write to file as backup
    pipelines=[
        ValidationPipeline(),
        PostgresPipeline(
            table="products",
            conflict_cols=["url"],
        ),
    ],
    settings=bitscrape.Settings(
        database_url="postgresql://user:pass@localhost/mydb"
    ),
)
```
