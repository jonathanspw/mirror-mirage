# Operations runbook

For operators deploying and maintaining Mirror Mirage in production.

## Files and locations

| Path | Owner | Mode | Purpose |
|------|-------|------|---------|
| `/etc/mirror-mirage/config.yaml` | root:root | 0644 | Operational config |
| `/etc/mirror-mirage/secrets.yaml` | mirror-mirage:mirror-mirage | 0600 | CDN credentials |
| `/etc/mirror-mirage/gcp-sa.json` | mirror-mirage:mirror-mirage | 0600 | GCP service account (if used) |
| `/var/lib/mirror-mirage/queue.db` | mirror-mirage:mirror-mirage | 0640 | Persistent job queue |
| `/var/lib/mirror-mirage/queue.db-wal` | mirror-mirage:mirror-mirage | 0640 | SQLite WAL |
| `/var/lib/mirror-mirage/queue.db-shm` | mirror-mirage:mirror-mirage | 0640 | SQLite shared memory |
| `/usr/lib/systemd/system/mirror-mirage.service` | root:root | 0644 | systemd unit |
| `/usr/lib/sysusers.d/mirror-mirage.conf` | root:root | 0644 | Creates the `mirror-mirage` user |
| `/usr/lib/tmpfiles.d/mirror-mirage.conf` | root:root | 0644 | Creates state dirs |

The daemon runs as the unprivileged `mirror-mirage` user with `ReadOnlyPaths=/srv/mirror`
and `ReadWritePaths=/var/lib/mirror-mirage /var/log/mirror-mirage`. It cannot modify mirror
content even if compromised.

## Initial deploy

1. Install the package (wheel or source — see [README](../README.md)).
2. `systemd-sysusers` creates the `mirror-mirage` user.
3. `systemd-tmpfiles --create` creates `/var/lib/mirror-mirage`.
4. Edit `/etc/mirror-mirage/config.yaml` for your watches and CDNs.
5. Edit `/etc/mirror-mirage/secrets.yaml` with real credentials. Verify:
   `stat -c '%a %U:%G' /etc/mirror-mirage/secrets.yaml` → `600 mirror-mirage:mirror-mirage`.
6. Validate without starting: `mirror-mirage --check --config /etc/mirror-mirage/config.yaml`.
7. Optional: dry-run for a few minutes to verify event coverage:
   `sudo -u mirror-mirage mirror-mirage --dry-run --config /etc/mirror-mirage/config.yaml`.
8. `systemctl enable --now mirror-mirage`.
9. Tail logs: `journalctl -u mirror-mirage -f`.

## Day-to-day commands

| Task | Command |
|------|---------|
| Status | `systemctl status mirror-mirage` |
| Live logs | `journalctl -u mirror-mirage -f` |
| Recent errors only | `journalctl -u mirror-mirage -p err --since '1 hour ago'` |
| Restart (picks up config changes) | `systemctl restart mirror-mirage` |
| Stop | `systemctl stop mirror-mirage` |
| Queue depth | `sudo -u mirror-mirage sqlite3 /var/lib/mirror-mirage/queue.db "SELECT status, COUNT(*) FROM jobs GROUP BY status"` |
| Recent jobs | `sudo -u mirror-mirage sqlite3 /var/lib/mirror-mirage/queue.db "SELECT id, cdn_name, status, attempts, datetime(next_attempt_at,'unixepoch') FROM jobs ORDER BY id DESC LIMIT 20"` |
| Inspect a job's URLs | `sudo -u mirror-mirage sqlite3 /var/lib/mirror-mirage/queue.db "SELECT urls_json FROM jobs WHERE id = 12345"` |

## Log fields

Every log line is structured `key=value`. Useful filters:

```sh
# All flushes that hit Fastly today
journalctl -u mirror-mirage --since today -g 'cdn=fastly_main' -g 'event=purge_ok'

# All retries
journalctl -u mirror-mirage -g 'event=purge_retry'

# Permanent failures
journalctl -u mirror-mirage -g 'event=purge_failed' -p err
```

Fields commonly emitted:

- `event` — one of `inotify_event`, `event_filtered`, `batch_enqueued`,
  `purge_attempt`, `purge_ok`, `purge_retry`, `purge_failed`.
- `cdn` — CDN binding name.
- `batch_size` — number of URLs in the batch.
- `attempt` — retry attempt number (1-indexed).
- `latency_ms` — provider call latency.
- `error` — error message on failure.
- `job_id` — primary key in the queue.
- `urls` — comma-separated URLs that landed in this batch, emitted on
  `purge_ok`, `purge_retry`, and `purge_failed`. Batches of more than
  five URLs are truncated to `<first-5>,+N-more` to keep the line
  grep-friendly; the full list is always recoverable from the queue
  (see the `urls_json` column on the matching `job_id`).
- `path`, `url` — only on individual event logs, not batch logs.

### Recovering the full URL list for a batch

When a `purge_*` line is truncated, the full list is on disk:

```sh
sudo -u mirror-mirage sqlite3 /var/lib/mirror-mirage/queue.db \
  "SELECT urls_json FROM jobs WHERE id = <job_id>"
```

This also works for any historical job — `done` and `failed` rows are
kept in the queue, not pruned.

## Common failures

### Daemon won't start: "secrets file has insecure permissions"

```sh
sudo chmod 0600 /etc/mirror-mirage/secrets.yaml
sudo chown mirror-mirage:mirror-mirage /etc/mirror-mirage/secrets.yaml
```

### Daemon won't start: "watch path does not exist"

The path in `config.yaml` is missing or the daemon can't see it. Check
`ReadOnlyPaths` in the systemd unit covers your mirror tree, and that
the directory actually exists.

### Daemon won't start: "no credentials for cdn 'X'"

Every CDN referenced in `config.yaml` must have a top-level entry in
`secrets.yaml`. Add it and restart.

### Daemon won't start: "inotify watch budget too small"

Mirror Mirage counts the directories it would watch and refuses to start when
that count exceeds `fs.inotify.max_user_watches`. The error message
includes the exact `sysctl` command to fix it:

```sh
sudo sysctl -w fs.inotify.max_user_watches=524288
```

To persist across reboots:

```sh
echo 'fs.inotify.max_user_watches = 524288' | \
  sudo tee /etc/sysctl.d/99-mirror-mirage.conf
```

A large file mirror — distro repos, ISO trees, archive content —
can easily contain hundreds of thousands of directories. The
default kernel limit varies by distribution and is often too low — 8192
on some systems, 65536 on others. `524288` is the comfortable upper
end and is what most CI and container hosts default to.

If you'd rather watch less of the tree, narrow `watches[].path` or
expand `watches[].ignore` to prune subtrees you don't serve via CDN.

### Cache freshness drift after a busy push

If you see logs like:

```
event=inotify_overflow kernel inotify queue overflowed; some
filesystem events were lost.
```

…the kernel's per-instance event queue filled up during a very large
burst. Mirror Mirage saw the overflow notification but could not recover the
specific events that were dropped — affected URLs may serve stale
content until the next change touches them.

Raise `fs.inotify.max_queued_events` similarly:

```sh
sudo sysctl -w fs.inotify.max_queued_events=65536
```

The kernel default is 16384, which a multi-million-file rsync can
exhaust transiently. Doubling or quadrupling is cheap (each queued
event is small).

### Queue is growing: jobs piling up in `pending`

The provider for that CDN is failing or rate-limited. Check
`journalctl -u mirror-mirage -g 'cdn=<name>' -p warning` for the error. Common
causes:

- **Expired credentials.** Update `secrets.yaml`, restart. The queue
  re-tries automatically.
- **CDN-side rate limiting.** Mirror Mirage backs off; queue depth will recover
  once the CDN catches up.
- **Wrong distribution / service ID.** Check `config.yaml`. Failed jobs
  end up in `status='failed'`.

### Queue has `failed` jobs

These exceeded `retry.max_attempts`. To inspect:

```sh
sudo -u mirror-mirage sqlite3 /var/lib/mirror-mirage/queue.db \
  "SELECT id, cdn_name, attempts, urls_json FROM jobs WHERE status='failed'"
```

To replay (after fixing the underlying problem):

```sh
sudo systemctl stop mirror-mirage
sudo -u mirror-mirage sqlite3 /var/lib/mirror-mirage/queue.db \
  "UPDATE jobs SET status='pending', attempts=0, next_attempt_at=strftime('%s','now') WHERE status='failed'"
sudo systemctl start mirror-mirage
```

### Daemon was offline during an rsync push

Mirror Mirage only sees events that occur while it is running. Files that landed
during downtime are not flushed automatically. Two recovery options:

1. **Targeted force-flush** (preferred for small recoveries): use the
   appropriate CDN CLI tool to purge the affected paths directly.
2. **Full purge for the affected CDN.** Disruptive (cache miss storm);
   only do this for catastrophic situations.

A future version of Mirror Mirage may ship a `mirror-mirage flush <path>` admin
subcommand for this; not yet implemented.

## Credential rotation

1. Generate the new credential in the CDN console.
2. Update `secrets.yaml` (preserve `0600 mirror-mirage:mirror-mirage`).
3. `systemctl restart mirror-mirage`. The queue persists across restart, so any
   in-flight retries pick up the new credentials.
4. Revoke the old credential in the CDN console.

## Updating config

There is no SIGHUP-driven reload yet. To change watches, CDNs, or daemon
tuning:

1. Edit `/etc/mirror-mirage/config.yaml`.
2. `mirror-mirage --check --config /etc/mirror-mirage/config.yaml` to validate before
   restarting.
3. `systemctl restart mirror-mirage`.

The persistent queue survives the restart; in-flight retries continue.

## Backing up state

The queue is operational state, not data — losing it just means losing
queued retries (which the daemon will quickly re-derive from new
filesystem events anyway). It is not normally worth backing up.

The two files that *do* need backup are `config.yaml` and `secrets.yaml`.
Store them in your standard secret-management workflow.

## Capacity planning

A rough rule of thumb on a 4-core mirror server: Mirror Mirage can comfortably
handle bursts of ~100k filesystem events per minute. Steady-state CPU is
near zero. Memory is dominated by the size of the in-buffer set of URLs
(a few MB even for very large bursts). The SQLite database stays small —
on the order of MB for a year of normal operation.

If you exceed those numbers, watch:
- `pending` queue depth over time.
- `latency_ms` on CDN calls.
- Per-CDN retry counts.

Tune `batch_quiet_window_seconds` upward if you see retry storms;
downward if freshness lags are unacceptable.
