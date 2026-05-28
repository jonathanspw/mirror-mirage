# Mirror Mirage

Inotify-driven CDN cache-flush daemon for filesystem mirrors.

Mirror Mirage watches one or more on-disk mirror trees with inotify. When files
are created, modified, or moved into place, it dispatches purge requests to
one or more CDNs (Fastly, AWS CloudFront, GCP Cloud CDN). Mirrors are
typically refreshed with `rsync --delay-updates`, so Mirror Mirage is built to
ignore the in-progress `.~tmp~/` staging directories and react only when
content is atomically renamed into place.

It's designed for any CDN-fronted file mirror — distro package repos, ISO
trees, large download archives — not tied to any particular project or
content format. The original use case was the AlmaLinux mirror network,
and the examples below use AlmaLinux URLs to illustrate, but the daemon
itself is content-agnostic.

A single filesystem change can fan out to several CDN purges — every CDN
binding configured for the affected path is notified, with the local
filesystem path translated to that CDN's public URL.

## Status

Pre-1.0. The daemon is feature-complete and the test suite passes end-to-end,
but config-file shape, CLI flags, and the on-disk queue schema may still
change before the first stable release.

## Features

- inotify-based watching with automatic recursion into newly-created
  subdirectories.
- Configurable per-watch ignore globs (designed for rsync staging dirs but
  works for any pattern).
- Per-CDN debouncing and bulk batching to minimize CDN API calls.
- SQLite-backed persistent job queue: in-flight and failed flushes survive
  daemon restarts.
- Exponential-backoff retry with configurable ceiling.
- Pluggable provider model — adding a new CDN is one class.
- systemd `Type=notify` service with hardened sandboxing.
- Structured journald logging.

## Requirements

- Linux with inotify (any modern distro).
- Python 3.12 or newer.
- systemd (for the service unit; the daemon runs fine without systemd too).

## Installation

System install (recommended for production):

```sh
python3 -m pip install /path/to/mirror-mirage      # or build a wheel
sudo install -d /etc/mirror-mirage
sudo install -m 0644 packaging/config.example.yaml /etc/mirror-mirage/config.yaml
sudo install -m 0600 packaging/secrets.example.yaml /etc/mirror-mirage/secrets.yaml
sudo install -m 0644 packaging/mirror-mirage.service /etc/systemd/system/mirror-mirage.service
sudo install -m 0644 packaging/mirror-mirage.sysusers /usr/lib/sysusers.d/mirror-mirage.conf
sudo install -m 0644 packaging/mirror-mirage.tmpfiles /usr/lib/tmpfiles.d/mirror-mirage.conf
sudo systemd-sysusers
sudo systemd-tmpfiles --create
sudo systemctl daemon-reload
sudo systemctl enable --now mirror-mirage.service
```

Development install:

```sh
git clone https://github.com/AlmaLinux/mirror-mirage
cd mirror-mirage
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev,journald]'
pytest
```

## Configuration

Two YAML files in `/etc/mirror-mirage/`:

- `config.yaml` — operational config. World-readable; safe for ops review.
- `secrets.yaml` — CDN credentials. Mode `0600`, owned by `mirror-mirage:mirror-mirage`.
  Mirror Mirage refuses to start if the mode is wider.

### `config.yaml`

```yaml
# Define each CDN once at the top. The name (e.g. fastly_main) is used to
# reference the CDN from watch bindings and to look up its credentials in
# secrets.yaml.
cdns:
  fastly_main:
    type: fastly
    service_id: SU1Z0isxPaozGVKXdv0eY

  cloudfront_us:
    type: cloudfront
    distribution_id: E2QWRUHEXAMPLE
    region: us-east-1

  gcp_eu:
    type: gcp_cdn
    project: almalinux-prod
    url_map: alma-prod-lb
    quota_per_minute: 500   # client-side pacing; default matches GCP's default quota

# Each watch is a filesystem root. When something changes inside it, every
# binding listed produces one CDN purge. url_prefix is concatenated with the
# path relative to `path:` to form the URL to flush.
watches:
  - path: /srv/mirror/almalinux
    ignore:
      - "**/.~tmp~/**"     # rsync --delay-updates staging directories
      - "**/.nfs*"         # silly-rename files
    # If false (the default), file deletions are ignored — only
    # creates/modifies/atomic-renames trigger CDN purges. Set to true
    # if you want the cached 404 for a removed URL to be invalidated
    # too. CloudFront bills per invalidation path, so leaving this off
    # can materially reduce cost on mirrors with high churn.
    flush_on_delete: false
    bindings:
      - cdn: fastly_main
        url_prefix: "https://repo.almalinux.org/almalinux/"
      - cdn: cloudfront_us
        url_prefix: "https://cdn-us.almalinux.org/almalinux/"

  - path: /srv/mirror/vault
    ignore:
      - "**/.~tmp~/**"
    bindings:
      - cdn: fastly_main
        url_prefix: "https://vault.almalinux.org/"

daemon:
  # Seconds of inotify quiet on a CDN before its accumulated URLs are flushed.
  batch_quiet_window_seconds: 10
  # Hard cap on coalesced URLs per CDN batch; reaching it triggers an early flush.
  batch_max_size: 500
  # Maximum time a URL can sit in the debounce buffer regardless of activity.
  batch_max_age_seconds: 60
  queue_db: /var/lib/mirror-mirage/queue.db
  retry:
    max_attempts: 8
    initial_backoff_seconds: 5
    max_backoff_seconds: 300
```

### `secrets.yaml`

```yaml
fastly_main:
  api_token: "fxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

cloudfront_us:
  access_key_id: "AKIAxxxxxxxxxxxxxxxx"
  secret_access_key: "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

gcp_eu:
  service_account_file: /etc/mirror-mirage/gcp-sa.json
```

The top-level keys must match CDN names from `config.yaml` exactly. Mirror Mirage
fails to start if any configured CDN is missing credentials.

### Validating config without starting

```sh
mirror-mirage --config /etc/mirror-mirage/config.yaml --check
```

Exits 0 on a valid config, non-zero with a diagnostic on the first problem.

### Dry-run / debug mode

```sh
mirror-mirage --config /etc/mirror-mirage/config.yaml --dry-run
```

In `--dry-run` mode every configured CDN is wrapped with a
`DryRunProvider` that logs each batch it would purge and returns
success without contacting any real CDN. The full pipeline still runs
end-to-end: inotify, the ignore-glob filter, URL mapping, batching, and
the persistent queue all behave normally — only the outbound API call
is suppressed.

Use this to verify event coverage on a staging server, or to confirm a
new config is correct before pointing the daemon at real credentials:

```sh
sudo -u mirror-mirage mirror-mirage --dry-run --config /etc/mirror-mirage/config.yaml
sudo journalctl -u mirror-mirage -g 'event=dry_run_purge' --since '1 minute ago'
```

## Operating the daemon

systemd lifecycle:

```sh
sudo systemctl start mirror-mirage
sudo systemctl status mirror-mirage
sudo systemctl restart mirror-mirage
sudo systemctl stop mirror-mirage
sudo journalctl -u mirror-mirage -f
```

Configuration reload is **not** supported via SIGHUP yet; restart the
service to apply config changes. The persistent queue survives restart.

Inspect the persistent queue:

```sh
sudo -u mirror-mirage sqlite3 /var/lib/mirror-mirage/queue.db \
  "SELECT id, cdn_name, status, attempts, next_attempt_at FROM jobs ORDER BY id DESC LIMIT 50"
```

See [docs/operations.md](docs/operations.md) for the full runbook.

## How event handling works

```
inotify → filter → mapper → batcher → SQLite queue → provider
```

1. **inotify watches** are added on every directory under each configured
   `watches[].path`, recursively. New subdirectories get a watch the moment
   they appear.
2. **Filter** drops events whose path matches an `ignore` glob and
   normalizes inotify event types into two outcomes: `INVALIDATE` (file is
   now live or replaced) and `DELETE` (file is gone).
3. **Mapper** computes the public URL for each binding on the watch that
   owns the changed file.
4. **Batcher** holds `(cdn, url)` pairs in per-CDN buffers. Each new event
   resets a quiet-window timer for that CDN; when the timer fires (or the
   batch hits `batch_max_size` URLs or `batch_max_age_seconds` age), the
   buffer is enqueued as one job.
5. **Queue** is a small SQLite database. Workers pull pending jobs and call
   the provider; failures reschedule with exponential backoff up to
   `retry.max_attempts`.
6. **Provider** for each CDN translates the URL list into the appropriate
   API call. See [docs/cdn-providers.md](docs/cdn-providers.md) for the
   per-CDN specifics.

## Documentation

- [docs/architecture.md](docs/architecture.md) — components, data flow,
  inotify-recursion race mitigation.
- [docs/cdn-providers.md](docs/cdn-providers.md) — per-CDN API details,
  batch limits, retryable error classes.
- [docs/operations.md](docs/operations.md) — deployment, log inspection,
  queue surgery, credential rotation, failure recovery.

## Development

```sh
pip install -e '.[dev,journald]'
pytest                  # all tests
pytest tests/test_filter.py -v
ruff check .
mypy src
```

Test layout mirrors the module layout — each `src/mirror_mirage/foo.py` has a
`tests/test_foo.py`.

## Releasing

CI runs on every push and pull request via [`.github/workflows/ci.yml`](.github/workflows/ci.yml):
pytest on Python 3.12 / 3.13 / 3.14, `ruff check` + `ruff format --check`,
`mypy src`, and `rpmlint` + SRPM build inside a Fedora container.

To cut a release:

```sh
# Bump pyproject.toml version, commit, then:
git tag v0.2.0
git push --tags
```

[`.github/workflows/release.yml`](.github/workflows/release.yml) fires on
the `v*` tag, verifies the tag matches `pyproject.toml`'s `version`,
builds the wheel + sdist with `python -m build`, publishes to PyPI via
trusted publishing, and creates a GitHub release with the artifacts
attached.

**Before the first release**, configure PyPI trusted publishing:

1. Reserve `mirror-mirage` on [PyPI](https://pypi.org/manage/account/publishing/)
   (or use an existing project).
2. Add a trusted publisher with:
   - Owner: the GitHub account/org that owns the repo
   - Repository: `mirror-mirage`
   - Workflow name: `release.yml`
   - Environment: `pypi`
3. In the GitHub repo: **Settings → Environments → New environment →
   `pypi`**. Enable "Required reviewers" so a human approves each upload.

No PyPI API tokens are stored anywhere; the workflow authenticates via
OIDC for each release.

## License

GPL-3.0-or-later.
