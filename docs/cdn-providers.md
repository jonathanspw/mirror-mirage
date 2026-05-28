# CDN provider details

Per-CDN notes on the API used, authentication, batch limits, and how Mirror Mirage
classifies errors as retryable or terminal.

All providers implement the same protocol:

```python
class CdnProvider(Protocol):
    async def purge(self, urls: list[str]) -> PurgeResult: ...

@dataclass(frozen=True)
class PurgeResult:
    ok: bool
    retryable: bool
    message: str
```

`urls` is the list of public URLs Mirror Mirage has decided need flushing. Each
provider is responsible for translating that list into the appropriate
API calls.

## Fastly

- **Type key:** `fastly`
- **Auth:** API token in `Fastly-Key` header. Token stored in
  `secrets.yaml` under `api_token`. The token is scoped to one Fastly
  service. Required token scopes:
  - `purge_select` for URL purges (always needed).
  - `global:read` (or wider) *additionally* when
    `purge_all_service_domains` is enabled — needed so the daemon can
    call `GET /service/{id}` and list the active version's domains.
- **Config required:** `service_id` — recorded for operator clarity
  (which CDN binding maps to which Fastly service); the URL-purge API
  itself does not require it in the request.
- **Config optional:**
  - `concurrency` (default `10`) — maximum in-flight purge requests at
    any moment. Tune up for very large batches if your Fastly account
    has elevated API quota.
  - `purge_all_service_domains` (default `false`) — when `true`, the
    daemon fetches the list of domains attached to ``service_id`` on
    first use and expands every purge across all of them, so one
    filesystem event flushes the cache entry on every hostname the
    service is serving. Useful when a single Fastly service fronts
    multiple mirror hostnames (e.g. `repo.example.org` *and*
    `mirror.example.org`) — saves having to list each one as a
    separate binding. Discovery is cached for the daemon's lifetime;
    restart to pick up newly-added domains.
- **API used:** Single-URL purge — `POST https://api.fastly.com/purge/{host}{path}[?{query}]`.
  Fastly does not expose a bulk URL purge endpoint; we issue one HTTP
  request per URL with bounded concurrency. (Fastly does support bulk
  purges via *surrogate keys*, but that requires the origin to emit
  `Surrogate-Key` headers — a different config model than the daemon's
  per-filesystem-path event flow.)
- **Idempotency:** safe. Re-purging an already-purged URL is a no-op.
- **Retryable status codes:** `408`, `425`, `429`, `500`, `502`, `503`,
  `504`, plus any transport-level error (connection reset, timeout).
- **Terminal status codes:** `400` (malformed URL), `401` (bad token —
  fix credentials), `403`, `404` (URL not part of this service).
- **Upstream docs:** <https://www.fastly.com/documentation/reference/api/purging/>

## AWS CloudFront

- **Type key:** `cloudfront`
- **Auth:** AWS access key + secret in `secrets.yaml` (`access_key_id`,
  `secret_access_key`). Alternative: omit credentials in `secrets.yaml` and
  rely on the daemon's IAM instance role / environment.
- **Config required:** `distribution_id`. `region` defaults to
  `us-east-1`; CloudFront control plane is global but boto3 still needs a
  region.
- **API used:** `cloudfront.create_invalidation(DistributionId, InvalidationBatch={...})`,
  called via `asyncio.to_thread` because boto3 is synchronous.
- **Batch limit:** CloudFront accepts up to 3,000 paths per invalidation;
  Mirror Mirage caps batches at `batch_max_size` (default 500) to keep latency
  predictable and to stay well under the limit. Each path counts toward
  the per-month free tier (1,000 paths free, then $0.005/path).
- **Path format:** CloudFront wants the *path* portion of the URL, not the
  full URL. The provider strips the scheme and host before submitting.
  E.g. `https://cdn.example.com/foo/bar.rpm` → `/foo/bar.rpm`.
- **Idempotency:** safe. Multiple invalidations on the same path are
  billed but functionally fine.
- **Retryable errors:** `Throttling`, `ThrottlingException`,
  `RequestLimitExceeded`, `ServiceUnavailable`, 5xx, transport errors.
- **Terminal errors:** `AccessDenied` (revoked credentials),
  `NoSuchDistribution`, `InvalidArgument`, `MalformedXML`.
- **Active-invalidation limit:** AWS allows up to 15 concurrent
  in-progress invalidations per distribution. Mirror Mirage's worker is
  single-threaded per distribution so this is not normally hit, but
  during retry storms the API may reject with
  `TooManyInvalidationsInProgress` — treated as retryable.
- **Upstream docs:** <https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/Invalidation.html>

## GCP Cloud CDN

- **Type key:** `gcp_cdn`
- **Auth:** Service account JSON file. Path stored in `secrets.yaml` under
  `service_account_file`. Token minted at startup via `google-auth`,
  refreshed automatically before expiry.
- **Config required:** `project` (GCP project ID), `url_map` (the load
  balancer URL map that fronts the CDN).
- **Config optional:** `quota_per_minute` (default `500`) — client-side
  cap on invalidation requests per rolling minute. Mirror Mirage will block
  excess requests until the window has room, instead of letting GCP
  reject them with `429`. Raise this only if your project has an
  elevated quota.
- **API used:** `compute.urlMaps.invalidateCache` —
  `POST https://compute.googleapis.com/compute/v1/projects/{project}/global/urlMaps/{url_map}/invalidateCache`
  with body `{"path": "/foo/bar", "host": "cdn.example.com"}`.
- **Batch limit:** **One path per call.** Mirror Mirage dispatches multiple
  invalidations concurrently with a bounded semaphore (default 10).
  A batch of N URLs becomes N API calls.
- **Path format:** path component of the URL; `host` is the URL's
  hostname, sent as a separate field.
- **Idempotency:** safe. Cache invalidations are absorbed if the path is
  already invalidated.
- **Quota:** GCP enforces invalidation quotas per project; default is
  500 paths/minute (overridable in the Cloud Console). Mirror Mirage paces
  itself client-side via ``quota_per_minute`` to stay under this cap.
  As a backstop, `429`/`RESOURCE_EXHAUSTED` responses are still treated
  as retryable, but in normal operation the daemon should never trip
  them.
- **Retryable errors:** `429`, `500`, `503`, `RESOURCE_EXHAUSTED`,
  transport errors.
- **Terminal errors:** `401`/`UNAUTHENTICATED` (rotate the service
  account), `403`/`PERMISSION_DENIED`, `404` (unknown URL map), `400`.
- **Upstream docs:** <https://cloud.google.com/cdn/docs/invalidating-cached-content>

## Adding a new provider

1. Create `src/mirror_mirage/providers/<name>.py`.
2. Define a class implementing `CdnProvider`. Constructor takes the
   relevant config + credentials. Async `purge(urls)` returns a
   `PurgeResult`.
3. Register the type key in `providers/__init__.py:build_providers`.
4. Document it in this file: API, auth, batch limits, retryable vs
   terminal error classification.
5. Add a `tests/test_providers.py` test class exercising request shape and
   error classification (typically with `respx` for HTTP providers).
