"""Tests for :mod:`mirror_mirage.config`.

These tests pin the contract documented in ``docs/architecture.md``:
strict validation, secrets file mode check, cross-reference checks
(binding → CDN, CDN → credentials, watch path → filesystem).
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from mirror_mirage.config import (
    Binding,
    CloudFrontCdn,
    Config,
    ConfigError,
    FastlyCdn,
    GcpCdnConfig,
    WatchConfig,
    load,
)

# ---------------------------------------------------------------------------
# Model-level parsing
# ---------------------------------------------------------------------------


class TestModelParsing:
    """Pydantic models accept their documented shapes."""

    def test_fastly_cdn_parses(self) -> None:
        cdn = FastlyCdn.model_validate({"type": "fastly", "service_id": "SUabc"})
        assert cdn.type == "fastly"
        assert cdn.service_id == "SUabc"

    def test_cloudfront_cdn_defaults_region(self) -> None:
        cdn = CloudFrontCdn.model_validate({"type": "cloudfront", "distribution_id": "E123"})
        assert cdn.region == "us-east-1"

    def test_gcp_cdn_parses(self) -> None:
        cdn = GcpCdnConfig.model_validate({"type": "gcp_cdn", "project": "p", "url_map": "u"})
        assert cdn.project == "p"

    def test_gcp_quota_defaults_to_500(self) -> None:
        cdn = GcpCdnConfig.model_validate({"type": "gcp_cdn", "project": "p", "url_map": "u"})
        assert cdn.quota_per_minute == 500

    def test_gcp_quota_can_be_overridden(self) -> None:
        cdn = GcpCdnConfig.model_validate(
            {
                "type": "gcp_cdn",
                "project": "p",
                "url_map": "u",
                "quota_per_minute": 2000,
            }
        )
        assert cdn.quota_per_minute == 2000

    def test_unknown_cdn_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            FastlyCdn.model_validate({"type": "fastly", "service_id": "x", "extra_key": 1})

    def test_watch_default_ignore_is_empty(self) -> None:
        wc = WatchConfig.model_validate(
            {"path": "/tmp", "bindings": [{"cdn": "a", "url_prefix": "https://x/"}]}
        )
        assert wc.ignore == []

    def test_flush_on_delete_defaults_to_false(self) -> None:
        wc = WatchConfig.model_validate(
            {"path": "/tmp", "bindings": [{"cdn": "a", "url_prefix": "https://x/"}]}
        )
        assert wc.flush_on_delete is False

    def test_flush_on_delete_can_be_enabled(self) -> None:
        wc = WatchConfig.model_validate(
            {
                "path": "/tmp",
                "flush_on_delete": True,
                "bindings": [{"cdn": "a", "url_prefix": "https://x/"}],
            }
        )
        assert wc.flush_on_delete is True

    def test_binding_requires_both_fields(self) -> None:
        with pytest.raises(ValidationError):
            Binding.model_validate({"cdn": "a"})


# ---------------------------------------------------------------------------
# load(): valid YAML
# ---------------------------------------------------------------------------


@pytest.fixture
def fixtures_dir(tmp_path: Path, mirror_tree: Path) -> Path:
    """A directory containing well-formed config.yaml + secrets.yaml."""
    config_yaml = textwrap.dedent(
        f"""
        cdns:
          fastly_main:
            type: fastly
            service_id: SUtestSvcId
          cloudfront_us:
            type: cloudfront
            distribution_id: E2QWRUHEXAMPLE
            region: us-east-1
        watches:
          - path: {mirror_tree}
            ignore:
              - "**/.~tmp~/**"
            bindings:
              - cdn: fastly_main
                url_prefix: https://repo.almalinux.org/almalinux/
              - cdn: cloudfront_us
                url_prefix: https://cdn-us.almalinux.org/almalinux/
        daemon:
          batch_quiet_window_seconds: 5
          batch_max_size: 500
          queue_db: {tmp_path}/queue.db
          retry:
            max_attempts: 8
            initial_backoff_seconds: 5
            max_backoff_seconds: 1800
        """
    )
    secrets_yaml = textwrap.dedent(
        """
        fastly_main:
          api_token: fxxx-test
        cloudfront_us:
          access_key_id: AKIATEST
          secret_access_key: secret-test
        """
    )
    cfg_path = tmp_path / "config.yaml"
    sec_path = tmp_path / "secrets.yaml"
    cfg_path.write_text(config_yaml)
    sec_path.write_text(secrets_yaml)
    os.chmod(sec_path, 0o600)
    return tmp_path


class TestLoadValid:
    def test_returns_config(self, fixtures_dir: Path) -> None:
        cfg = load(fixtures_dir / "config.yaml", fixtures_dir / "secrets.yaml")
        assert isinstance(cfg, Config)
        assert set(cfg.cdns) == {"fastly_main", "cloudfront_us"}
        assert len(cfg.watches) == 1

    def test_credentials_attached(self, fixtures_dir: Path) -> None:
        cfg = load(fixtures_dir / "config.yaml", fixtures_dir / "secrets.yaml")
        assert cfg.credentials["fastly_main"]["api_token"] == "fxxx-test"
        assert cfg.credentials["cloudfront_us"]["access_key_id"] == "AKIATEST"


# ---------------------------------------------------------------------------
# load(): validation failures
# ---------------------------------------------------------------------------


def _write(path: Path, data: dict[str, object], mode: int | None = None) -> None:
    path.write_text(yaml.safe_dump(data))
    if mode is not None:
        os.chmod(path, mode)


class TestLoadFailures:
    def test_secrets_permissive_mode_rejected(self, tmp_path: Path, mirror_tree: Path) -> None:
        cfg_path = tmp_path / "config.yaml"
        sec_path = tmp_path / "secrets.yaml"
        _write(
            cfg_path,
            {
                "cdns": {
                    "fastly_main": {"type": "fastly", "service_id": "x"},
                },
                "watches": [
                    {
                        "path": str(mirror_tree),
                        "bindings": [{"cdn": "fastly_main", "url_prefix": "https://x/"}],
                    }
                ],
            },
        )
        _write(sec_path, {"fastly_main": {"api_token": "x"}}, mode=0o644)
        with pytest.raises(ConfigError, match="permission"):
            load(cfg_path, sec_path)

    def test_unknown_cdn_in_binding(self, tmp_path: Path, mirror_tree: Path) -> None:
        cfg_path = tmp_path / "config.yaml"
        sec_path = tmp_path / "secrets.yaml"
        _write(
            cfg_path,
            {
                "cdns": {
                    "fastly_main": {"type": "fastly", "service_id": "x"},
                },
                "watches": [
                    {
                        "path": str(mirror_tree),
                        "bindings": [{"cdn": "nonexistent", "url_prefix": "https://x/"}],
                    }
                ],
            },
        )
        _write(sec_path, {"fastly_main": {"api_token": "x"}}, mode=0o600)
        with pytest.raises(ConfigError, match="nonexistent"):
            load(cfg_path, sec_path)

    def test_cdn_without_credentials(self, tmp_path: Path, mirror_tree: Path) -> None:
        cfg_path = tmp_path / "config.yaml"
        sec_path = tmp_path / "secrets.yaml"
        _write(
            cfg_path,
            {
                "cdns": {
                    "fastly_main": {"type": "fastly", "service_id": "x"},
                    "cloudfront_us": {
                        "type": "cloudfront",
                        "distribution_id": "E1",
                    },
                },
                "watches": [
                    {
                        "path": str(mirror_tree),
                        "bindings": [{"cdn": "fastly_main", "url_prefix": "https://x/"}],
                    }
                ],
            },
        )
        _write(sec_path, {"fastly_main": {"api_token": "x"}}, mode=0o600)
        with pytest.raises(ConfigError, match="cloudfront_us"):
            load(cfg_path, sec_path)

    def test_nonexistent_watch_path(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.yaml"
        sec_path = tmp_path / "secrets.yaml"
        bogus = tmp_path / "no-such-mirror"
        _write(
            cfg_path,
            {
                "cdns": {
                    "fastly_main": {"type": "fastly", "service_id": "x"},
                },
                "watches": [
                    {
                        "path": str(bogus),
                        "bindings": [{"cdn": "fastly_main", "url_prefix": "https://x/"}],
                    }
                ],
            },
        )
        _write(sec_path, {"fastly_main": {"api_token": "x"}}, mode=0o600)
        with pytest.raises(ConfigError, match="does not exist"):
            load(cfg_path, sec_path)

    def test_yaml_parse_error(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.yaml"
        sec_path = tmp_path / "secrets.yaml"
        cfg_path.write_text("cdns: [not: a, mapping\n")
        sec_path.write_text("{}\n")
        os.chmod(sec_path, 0o600)
        with pytest.raises(ConfigError):
            load(cfg_path, sec_path)
