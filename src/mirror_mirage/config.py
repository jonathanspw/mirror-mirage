"""Configuration models and loader.

Two YAML files compose the runtime configuration:

* ``config.yaml`` — operational settings (CDNs, watches, daemon tuning).
* ``secrets.yaml`` — credentials, keyed by CDN name. Must be mode ``0600``
  and owned by the daemon's user.

The public entrypoint is :func:`load`, which parses both files, validates
them, and returns a :class:`Config` whose ``cdns`` mapping is annotated
with credentials.

Validation is strict: the loader refuses to return a ``Config`` if any
binding references an unknown CDN, any CDN lacks credentials, any watch
path does not exist on disk, or the secrets file has overly permissive
mode.
"""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class FastlyCdn(BaseModel):
    """Fastly CDN configuration block.

    When ``purge_all_service_domains`` is True, the daemon calls Fastly's
    API at first use to discover every domain attached to ``service_id``
    and expands each URL purge across all of them — so a single
    filesystem event flushes the cache entry on every host the Fastly
    service is serving, without needing one binding per host in this
    file. The API token must include ``global:read`` (or wider) scope in
    addition to ``purge_select`` for discovery to succeed; most
    automation tokens already have this.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["fastly"]
    service_id: str
    purge_all_service_domains: bool = False


class CloudFrontCdn(BaseModel):
    """AWS CloudFront CDN configuration block."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["cloudfront"]
    distribution_id: str
    region: str = "us-east-1"


class GcpCdnConfig(BaseModel):
    """GCP Cloud CDN configuration block.

    ``quota_per_minute`` enforces a client-side cap on invalidation
    requests so that Mirage does not exceed the project's per-minute
    invalidation quota. The default of 500 matches GCP's default quota
    of 500 paths/minute; raise this if your project has an elevated
    quota. Mirage blocks (does not drop) excess work, so a burst above
    quota is paced out across subsequent minutes.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["gcp_cdn"]
    project: str
    url_map: str
    quota_per_minute: int = 500


CdnConfig = Annotated[
    FastlyCdn | CloudFrontCdn | GcpCdnConfig,
    Field(discriminator="type"),
]


class Binding(BaseModel):
    """Single CDN binding inside a watch."""

    model_config = ConfigDict(extra="forbid")

    cdn: str
    url_prefix: str


class WatchConfig(BaseModel):
    """One filesystem root to watch and its CDN bindings.

    ``flush_on_delete`` controls whether file removals (``IN_DELETE`` /
    ``IN_MOVED_FROM``) trigger CDN purges. The default is ``False``:
    deletions are silently ignored. Set to ``True`` if you want the CDN
    cache for the removed URL to be invalidated too — note that
    CloudFront charges per invalidation path, so leaving this off can
    materially reduce cost on mirrors with high churn.
    """

    model_config = ConfigDict(extra="forbid")

    path: Path
    ignore: list[str] = []
    flush_on_delete: bool = False
    bindings: list[Binding]


class RetryConfig(BaseModel):
    """Retry policy for failed CDN purges."""

    model_config = ConfigDict(extra="forbid")

    max_attempts: int = 8
    initial_backoff_seconds: float = 5.0
    max_backoff_seconds: float = 300.0


class DaemonConfig(BaseModel):
    """Daemon-wide tuning."""

    model_config = ConfigDict(extra="forbid")

    batch_quiet_window_seconds: float = 10.0
    batch_max_size: int = 500
    batch_max_age_seconds: float = 60.0
    queue_db: Path = Path("/var/lib/mirror-mirage/queue.db")
    retry: RetryConfig = RetryConfig()


class Config(BaseModel):
    """The fully-validated runtime configuration.

    The ``credentials`` mapping is populated by :func:`load`; tests may
    construct a ``Config`` directly with credentials inline.
    """

    model_config = ConfigDict(extra="forbid")

    cdns: dict[str, CdnConfig]
    watches: list[WatchConfig]
    daemon: DaemonConfig = DaemonConfig()
    credentials: dict[str, dict[str, str]] = Field(default_factory=dict)


class ConfigError(Exception):
    """Raised when configuration cannot be loaded or validated."""


def load(config_path: Path, secrets_path: Path) -> Config:
    """Parse ``config_path`` and ``secrets_path``, validate, and return a Config.

    Raises :class:`ConfigError` on any of:
    * YAML parse failure
    * Schema validation failure (unknown fields, bad types)
    * ``secrets_path`` mode wider than ``0600``
    * A binding referencing a CDN not defined under ``cdns:``
    * A CDN with no entry in ``secrets.yaml``
    * A ``watches[].path`` that does not exist on disk
    """
    # Mode check first — refuse to even read a world-readable secrets file.
    try:
        st = secrets_path.stat()
    except OSError as e:
        raise ConfigError(f"cannot stat secrets file {secrets_path}: {e}") from e
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        raise ConfigError(
            f"secrets file {secrets_path} has insecure permission {mode:#o}; "
            f"expected mode 0600 (group/world bits must be clear)"
        )

    try:
        config_text = config_path.read_text()
    except OSError as e:
        raise ConfigError(f"cannot read config file {config_path}: {e}") from e
    try:
        config_data = yaml.safe_load(config_text) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"failed to parse {config_path}: {e}") from e

    try:
        secrets_text = secrets_path.read_text()
    except OSError as e:
        raise ConfigError(f"cannot read secrets file {secrets_path}: {e}") from e
    try:
        secrets_data: Any = yaml.safe_load(secrets_text) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"failed to parse {secrets_path}: {e}") from e
    if not isinstance(secrets_data, dict):
        raise ConfigError(f"secrets file {secrets_path} must be a mapping")

    try:
        cfg = Config.model_validate(config_data)
    except ValidationError as e:
        raise ConfigError(f"invalid configuration: {e}") from e

    cdn_names = set(cfg.cdns)
    for watch in cfg.watches:
        if not watch.path.exists():
            raise ConfigError(f"watch path {watch.path} does not exist")
        for binding in watch.bindings:
            if binding.cdn not in cdn_names:
                raise ConfigError(
                    f"watch {watch.path}: binding references unknown cdn "
                    f"{binding.cdn!r}; defined cdns: {sorted(cdn_names)}"
                )

    for cdn_name in cfg.cdns:
        creds = secrets_data.get(cdn_name)
        if not creds or not isinstance(creds, dict):
            raise ConfigError(f"no credentials for cdn {cdn_name!r} in {secrets_path}")
        cfg.credentials[cdn_name] = {str(k): str(v) for k, v in creds.items()}

    return cfg
