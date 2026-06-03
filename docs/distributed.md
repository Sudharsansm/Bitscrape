# Distributed Crawling

Bitscrape supports distributed crawling via a **Redis-backed shared queue**.
Multiple worker processes all pull from and push to the same queue, so you can
scale horizontally by simply starting more workers.

---

## How It Works

In single-process mode, the scheduler uses an in-memory `asyncio.PriorityQueue`.
In distributed mode, it uses a Redis **sorted set** as the queue:

```
Worker 1 ──┐
Worker 2 ──┼──► Redis Queue ◄──► PostgreSQL
Worker 3 ──┘
```

- All workers share one queue — no duplicate work
- If a worker crashes, its pending requests stay in Redis
- New workers resume from where the crashed worker left off
- The URL deduplication filter is also stored in Redis (a Redis set)

---

## Requirements

```bash
pip install "bitscrape[redis]"
```

You need a running Redis instance:

```bash
# Docker (easiest)
docker run -d -p 6379:6379 redis:7

# Or use a managed service:
# - Redis Cloud: https://redis.com/try-free/
# - Upstash: https://upstash.com/
# - AWS ElastiCache
```

---

## Enable Distributed Mode

### Via environment variable

```bash
export BITSCRAPE_SCHEDULER_USE_REDIS=true
export BITSCRAPE_REDIS_URL=redis://localhost:6379/0
```

### Via Settings

```python
settings = bitscrape.Settings(
    scheduler_use_redis=True,
    redis_url="redis://localhost:6379/0",
)
```

---

## Running Multiple Workers

Start as many workers as you need — they all connect to the same Redis:

```bash
# Terminal 1
BITSCRAPE_SCHEDULER_USE_REDIS=true bitscrape crawl spiders/products.py

# Terminal 2
BITSCRAPE_SCHEDULER_USE_REDIS=true bitscrape crawl spiders/products.py

# Terminal 3
BITSCRAPE_SCHEDULER_USE_REDIS=true bitscrape crawl spiders/products.py
```

Or with Docker Compose:

```yaml
# docker-compose.yml
version: "3.9"

services:
  redis:
    image: redis:7
    ports: ["6379:6379"]

  worker:
    build: .
    command: bitscrape crawl spiders/products.py -o /data/products.jsonl
    environment:
      - BITSCRAPE_SCHEDULER_USE_REDIS=true
      - BITSCRAPE_REDIS_URL=redis://redis:6379/0
      - BITSCRAPE_DATABASE_URL=postgresql://user:pass@postgres/mydb
    volumes:
      - ./output:/data
    deploy:
      replicas: 4       # 4 workers
    depends_on:
      - redis

  postgres:
    image: postgres:16
    environment:
      POSTGRES_PASSWORD: pass
      POSTGRES_USER: user
      POSTGRES_DB: mydb
```

```bash
docker-compose up --scale worker=4
```

---

## Redis Queue Keys

Bitscrape uses these Redis keys:

| Key | Type | Description |
|-----|------|-------------|
| `bitscrape:queue` | Sorted set | The crawl request queue |
| `bitscrape:dupes` | Set | Seen URL fingerprints |

To clear the queue and start fresh:

```bash
redis-cli DEL bitscrape:queue bitscrape:dupes
```

---

## Resuming After a Crash

Because the queue persists in Redis, you can resume a crawl after a failure:

```bash
# Start a crawl
bitscrape crawl spiders/products.py &

# It crashes / you kill it
# Queue still has pending URLs in Redis

# Just restart — it picks up where it left off
bitscrape crawl spiders/products.py
```

Already-scraped URLs are in the dupes set, so they won't be re-crawled.

---

## Scaling on Kubernetes

Example Kubernetes deployment:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: bitscrape-worker
spec:
  replicas: 10
  selector:
    matchLabels:
      app: bitscrape-worker
  template:
    metadata:
      labels:
        app: bitscrape-worker
    spec:
      containers:
        - name: worker
          image: yourorg/bitscrape:0.1.0
          command: ["bitscrape", "crawl", "spiders/products.py"]
          env:
            - name: BITSCRAPE_SCHEDULER_USE_REDIS
              value: "true"
            - name: BITSCRAPE_REDIS_URL
              valueFrom:
                secretKeyRef:
                  name: bitscrape-secrets
                  key: redis-url
            - name: BITSCRAPE_DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: bitscrape-secrets
                  key: database-url
```

Use the **Kubernetes Horizontal Pod Autoscaler** to scale based on Redis
queue depth.

---

## Dockerfile

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install "bitscrape[full]"

COPY spiders/ ./spiders/

CMD ["bitscrape", "crawl", "spiders/myspider.py"]
```
