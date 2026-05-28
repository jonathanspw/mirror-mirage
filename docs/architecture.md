# Architecture

This document is aimed at contributors. End users should start with the
top-level [README](../README.md).

## Goals

- One filesystem event can fan out to multiple CDN purges.
- Bursts of events (a 50,000-file rsync push) coalesce into a small number
  of bulk purge calls per CDN — not 50,000 individual API calls.
- A daemon restart or a CDN outage must not lose pending purges. Failed
  purges retry until they succeed or hit a configured cap.
- New subdirectories created inside a watch root start being watched
  automatically — without losing files created during the gap between
  `mkdir` and watch registration.
- Each component is independently testable; the pipeline composes pure
  functions where possible.

## Data flow

```
                ┌─────────────┐
                │  inotify    │  (asyncinotify, asyncio-native)
                └──────┬──────┘
                       │ raw inotify events
                       ▼
                ┌─────────────┐
                │  Filter     │  ignore globs, event-kind normalization
                └──────┬──────┘
                       │ FsEvent(kind, abs_path, watch_root)
                       ▼
                ┌─────────────┐
                │  Mapper     │  fs path → public URL, per binding
                └──────┬──────┘
                       │ (cdn_name, url) tuples (fan-out)
                       ▼
                ┌─────────────┐
                │  Batcher    │  per-CDN debouncing + size/age caps
                └──────┬──────┘
                       │ Batch(cdn_name, urls)
                       ▼
                ┌─────────────┐
                │  Queue      │  SQLite, durable, retryable
                └──────┬──────┘
                       │ ready jobs
                       ▼
                ┌─────────────┐
                │  Providers  │  fastly | cloudfront | gcp_cdn
                └──────┬──────┘
                       │
                       ▼
                  CDN APIs
```

A single asyncio event loop drives everything. There are no worker threads
except those `asyncio.to_thread` spawns for blocking boto3 calls.

## Components

### `watcher.py` — inotify

Manages an `asyncinotify.Inotify` instance. On startup, walks each configured
watch root and adds a watch on every directory it finds. The mask is:

```
IN_MOVED_TO | IN_CLOSE_WRITE | IN_DELETE | IN_MOVED_FROM
| IN_CREATE | IN_MOVE_SELF | IN_DELETE_SELF
```

`IN_MOVED_TO` is the primary signal for rsync `--delay-updates`: rsync
stages new content in `.~tmp~/` and atomically renames it into place.
`IN_CLOSE_WRITE` covers in-place writes (manual edits, non-rsync tools).
`IN_DELETE` and `IN_MOVED_FROM` cover removals.

#### Recursion and the inotify race

When `IN_CREATE | IN_ISDIR` fires, there is a window between the directory
being created and Mirror Mirage adding a watch on it. Files created inside that
window would otherwise be missed. The mitigation:

1. Add the watch on the new directory immediately.
2. Then scan the directory's contents and emit synthetic `INVALIDATE`
   events for any files/subdirectories already present.
3. For any subdirectories discovered, recurse.

`os.scandir` is used for the post-watch rescan because it's cheap and gives
us file type info without an extra `stat`.

### `filter.py` — event filtering and normalization

Two responsibilities:

1. **Drop ignored events.** A watch's `ignore` globs are evaluated against
   the path relative to the watch root. The default pattern
   `**/.~tmp~/**` covers rsync staging. Defensive: even though the watcher
   never adds a watch on a `.~tmp~/` directory, events inside one could
   still surface if rsync writes to an existing target directory.
2. **Normalize event kinds.** inotify has many event types; downstream
   only cares about two:
   - `INVALIDATE` — the file at this path is now live or was replaced.
     Sources: `IN_MOVED_TO`, `IN_CLOSE_WRITE`.
   - `DELETE` — the file at this path is gone. Sources: `IN_DELETE`,
     `IN_MOVED_FROM`. Dropped at this stage when the owning watch has
     `flush_on_delete: false` (the default), so deletions never reach
     the mapper or any provider.

### `mapper.py` — path-to-URL transform

For each `FsEvent`, look up the watch and emit one `(cdn_name, url)` tuple
per binding:

```python
rel = abs_path.relative_to(watch.path)
url = binding.url_prefix.rstrip("/") + "/" + rel.as_posix()
```

The function does not consult any state beyond the static binding list, so
it is pure and unit-testable in isolation.

### `batcher.py` — coalescing

One buffer per CDN, holding a `set[str]` of URLs. Three flush triggers:

1. **Quiet window** — `batch_quiet_window_seconds` of inactivity on that
   CDN. Implemented with a per-CDN asyncio task that sleeps and re-arms on
   new arrivals.
2. **Size cap** — buffer reaches `batch_max_size` URLs. Forces immediate
   flush regardless of timing.
3. **Age cap** — the oldest URL in the buffer is older than
   `batch_max_age_seconds`. Prevents indefinite postponement during
   continuous activity (e.g. a constantly-churning mirror).

Flushing is "enqueue a job into the persistent queue, then reset the
buffer." The batcher does not call CDN APIs itself — the queue does, so
in-flight work survives a crash.

CDN isolation: a flood on `cdn=fastly_main` must not block flushes for
`cdn=cloudfront_us`. Each CDN runs its own debounce loop.

### `queue.py` — durable retry

`aiosqlite` over a single `jobs` table:

```
id              INTEGER PRIMARY KEY
cdn_name        TEXT NOT NULL
urls_json       TEXT NOT NULL    -- JSON array
attempts        INTEGER NOT NULL DEFAULT 0
next_attempt_at REAL NOT NULL    -- Unix timestamp
created_at      REAL NOT NULL
status          TEXT NOT NULL    -- pending | in_flight | done | failed
```

WAL mode for crash safety. One worker per CDN:

```
while running:
    job = await claim_ready_job(cdn_name)        # in_flight, returns row
    result = await provider.purge(job.urls)
    if result.ok:
        await mark_done(job.id)
    elif result.retryable and job.attempts < max_attempts:
        await reschedule(job.id, exponential_backoff(job.attempts))
    else:
        await mark_failed(job.id, result.message)
```

Exponential backoff:
`min(initial_backoff * 2**(attempts - 1), max_backoff)`.

On startup, any `in_flight` job is reset to `pending` — purges are
idempotent on all three CDNs, so an interrupted job is safe to re-run.

### `providers/` — per-CDN clients

Common protocol:

```python
class CdnProvider(Protocol):
    async def purge(self, urls: list[str]) -> PurgeResult: ...
```

`PurgeResult` is a small dataclass: `ok`, `retryable`, `message`. See
[cdn-providers.md](cdn-providers.md) for per-CDN details.

### `config.py` — config loading

Pydantic v2 models. Load steps:

1. Parse `config.yaml` and `secrets.yaml`.
2. Validate the secrets file has mode `0600` and is owned by the running
   user. Refuse otherwise.
3. Merge: each `cdns[name]` entry gets its corresponding `secrets[name]`
   attached as a private field.
4. Cross-check: every `watches[].bindings[].cdn` must resolve to a defined
   CDN; every CDN must have credentials; every `watches[].path` must exist
   on disk.

Refuses to start on any validation failure with a clear diagnostic.

### `daemon.py` — orchestration

Wires everything together:

```python
async def run(cfg: Config, *, dry_run: bool = False) -> None:
    queue = await PersistentQueue.open(cfg.daemon.queue_db)
    providers = build_providers(cfg, dry_run=dry_run)
    batcher = Batcher(queue, cfg.daemon)
    mapper = Mapper(cfg.watches)
    filter_ = EventFilter(cfg.watches)
    watcher = InotifyWatcher(cfg.watches)

    async with TaskGroup() as tg:
        for cdn_name, provider in providers.items():
            tg.create_task(queue.worker(cdn_name, provider, cfg.daemon.retry))
        tg.create_task(batcher.run())
        tg.create_task(_pipeline(watcher, filter_, mapper, batcher))
```

`__main__.py` handles argument parsing, signal handling (graceful shutdown
on SIGTERM/SIGINT), `sd_notify("READY=1")`, and exception logging.

## Concurrency model

- One asyncio event loop.
- One asyncio task per: inotify reader, batcher coordinator, per-CDN
  worker.
- boto3 calls are wrapped with `asyncio.to_thread`. `httpx.AsyncClient` is
  used directly elsewhere.
- No shared mutable state across tasks except via asyncio primitives
  (queues, locks) or the SQLite WAL.

## Failure semantics

- **Daemon crash mid-flush:** the in-flight job is reset to `pending` at
  startup and re-run. Purges are idempotent.
- **CDN 5xx or 429:** retryable; job is rescheduled with exponential
  backoff.
- **CDN 4xx (other than 429):** non-retryable; job is marked `failed` and
  logged. Operator inspects `journalctl -u mirror-mirage` and the queue.
- **Credentials revoked:** the provider returns retryable=true on 401/403?
  We treat 401/403 as **non-retryable** because retries will never
  succeed. The fix is to update `secrets.yaml` and restart.
- **Disk full on `/var/lib/mirror-mirage`:** queue inserts fail; the batcher logs
  loudly and drops the batch.

## Testability

Component boundaries chosen to keep most tests synchronous and offline:

- `filter` and `mapper` are pure functions.
- `batcher` is async but takes a fake "enqueue" callable; tests use
  `asyncio.sleep(0)` and the event loop's mocked clock.
- `queue` is exercised against an in-memory SQLite (`:memory:`) or a
  tmp_path-backed file.
- Providers are tested with `respx` (Fastly, GCP) and a hand-rolled
  CloudFront client stub.
- The watcher has one integration test against a real inotify + tmp_path
  that simulates an rsync push.
- One end-to-end test composes everything with mock providers.
