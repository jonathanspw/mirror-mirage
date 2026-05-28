"""Tests for :mod:`mirror_mirage.providers`.

Verifies request shape, batching, and error classification for each
provider against the rules in ``docs/cdn-providers.md``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from mirror_mirage.providers.base import PurgeResult
from mirror_mirage.providers.cloudfront import CloudFrontProvider
from mirror_mirage.providers.dry_run import DryRunProvider
from mirror_mirage.providers.fastly import FastlyProvider
from mirror_mirage.providers.gcp import GcpCdnProvider

# ---------------------------------------------------------------------------
# Fastly
# ---------------------------------------------------------------------------


class TestFastlyProvider:
    """Per-URL purge: POST https://api.fastly.com/purge/{host}{path}."""

    @respx.mock
    async def test_calls_per_url_purge_endpoint(self) -> None:
        # One route per URL.
        r_a = respx.post("https://api.fastly.com/purge/repo.example.com/a").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        r_b = respx.post("https://api.fastly.com/purge/repo.example.com/b").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        async with httpx.AsyncClient() as client:
            p = FastlyProvider(service_id="svc-id", api_token="fxxx-test", client=client)
            result = await p.purge(["https://repo.example.com/a", "https://repo.example.com/b"])
        assert result.ok is True
        assert r_a.called and r_b.called

    @respx.mock
    async def test_one_call_per_url(self) -> None:
        route = respx.post(
            url__regex=r"https://api\.fastly\.com/purge/repo\.example\.com/\d+"
        ).mock(return_value=httpx.Response(200, json={"status": "ok"}))
        async with httpx.AsyncClient() as client:
            p = FastlyProvider(service_id="svc-id", api_token="fxxx-test", client=client)
            urls = [f"https://repo.example.com/{i}" for i in range(20)]
            result = await p.purge(urls)
        assert result.ok is True
        assert route.call_count == 20

    @respx.mock
    async def test_sends_token_in_header(self) -> None:
        route = respx.post("https://api.fastly.com/purge/repo.example.com/a").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        async with httpx.AsyncClient() as client:
            p = FastlyProvider(service_id="svc-id", api_token="fxxx-test", client=client)
            await p.purge(["https://repo.example.com/a"])
        assert route.calls[0].request.headers.get("Fastly-Key") == "fxxx-test"

    @respx.mock
    async def test_query_string_preserved(self) -> None:
        route = respx.post("https://api.fastly.com/purge/repo.example.com/x?v=1").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        async with httpx.AsyncClient() as client:
            p = FastlyProvider(service_id="svc-id", api_token="fxxx-test", client=client)
            await p.purge(["https://repo.example.com/x?v=1"])
        assert route.called

    @respx.mock
    async def test_429_is_retryable(self) -> None:
        respx.post(url__regex=r"https://api\.fastly\.com/purge/.*").mock(
            return_value=httpx.Response(429, json={"msg": "rate limited"})
        )
        async with httpx.AsyncClient() as client:
            p = FastlyProvider(service_id="svc-id", api_token="fxxx-test", client=client)
            result = await p.purge(["https://repo.example.com/a"])
        assert result.ok is False
        assert result.retryable is True

    @respx.mock
    async def test_500_is_retryable(self) -> None:
        respx.post(url__regex=r"https://api\.fastly\.com/purge/.*").mock(
            return_value=httpx.Response(503)
        )
        async with httpx.AsyncClient() as client:
            p = FastlyProvider(service_id="svc-id", api_token="fxxx-test", client=client)
            result = await p.purge(["https://repo.example.com/a"])
        assert result.retryable is True

    @respx.mock
    async def test_401_is_terminal(self) -> None:
        respx.post(url__regex=r"https://api\.fastly\.com/purge/.*").mock(
            return_value=httpx.Response(401)
        )
        async with httpx.AsyncClient() as client:
            p = FastlyProvider(service_id="svc-id", api_token="fxxx-test", client=client)
            result = await p.purge(["https://repo.example.com/a"])
        assert result.ok is False
        assert result.retryable is False

    @respx.mock
    async def test_transport_error_is_retryable(self) -> None:
        respx.post(url__regex=r"https://api\.fastly\.com/purge/.*").mock(
            side_effect=httpx.ConnectError("boom")
        )
        async with httpx.AsyncClient() as client:
            p = FastlyProvider(service_id="svc-id", api_token="fxxx-test", client=client)
            result = await p.purge(["https://repo.example.com/a"])
        assert result.retryable is True

    @respx.mock
    async def test_mixed_results_terminal_wins(self) -> None:
        # If any URL hits a terminal error, the whole batch is terminal.
        respx.post("https://api.fastly.com/purge/repo.example.com/a").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        respx.post("https://api.fastly.com/purge/repo.example.com/b").mock(
            return_value=httpx.Response(401)
        )
        async with httpx.AsyncClient() as client:
            p = FastlyProvider(service_id="svc-id", api_token="fxxx-test", client=client)
            result = await p.purge(["https://repo.example.com/a", "https://repo.example.com/b"])
        assert result.ok is False
        assert result.retryable is False


SERVICE_DETAILS: dict[str, object] = {
    "id": "svc-id",
    "versions": [
        {"number": 1, "active": False},
        {"number": 42, "active": True},
    ],
}
DOMAINS_PAYLOAD: list[dict[str, str]] = [
    {"name": "repo.example.com"},
    {"name": "mirror.example.com"},
    {"name": "alt.example.com"},
]


class TestFastlyPurgeAllServiceDomains:
    """``purge_all_service_domains=True`` discovers service domains and
    expands each input URL across all of them."""

    @respx.mock
    async def test_expansion_purges_every_domain(self) -> None:
        respx.get("https://api.fastly.com/service/svc-id").mock(
            return_value=httpx.Response(200, json=SERVICE_DETAILS)
        )
        respx.get("https://api.fastly.com/service/svc-id/version/42/domain").mock(
            return_value=httpx.Response(200, json=DOMAINS_PAYLOAD)
        )
        # All purge endpoints succeed.
        purge_route = respx.post(url__regex=r"https://api\.fastly\.com/purge/[^/]+/foo\.rpm").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )

        async with httpx.AsyncClient() as client:
            p = FastlyProvider(
                service_id="svc-id",
                api_token="fxxx-test",
                client=client,
                purge_all_service_domains=True,
            )
            result = await p.purge(["https://repo.example.com/foo.rpm"])

        assert result.ok is True
        # Three domains → three purge calls (the URL's own host is one of them
        # and is deduplicated, not double-purged).
        assert purge_route.call_count == 3
        hosts_purged = {call.request.url.path.split("/")[2] for call in purge_route.calls}
        assert hosts_purged == {
            "repo.example.com",
            "mirror.example.com",
            "alt.example.com",
        }

    @respx.mock
    async def test_discovery_cached_across_purges(self) -> None:
        details = respx.get("https://api.fastly.com/service/svc-id").mock(
            return_value=httpx.Response(200, json=SERVICE_DETAILS)
        )
        domains = respx.get("https://api.fastly.com/service/svc-id/version/42/domain").mock(
            return_value=httpx.Response(200, json=DOMAINS_PAYLOAD)
        )
        respx.post(url__regex=r"https://api\.fastly\.com/purge/.*").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )

        async with httpx.AsyncClient() as client:
            p = FastlyProvider(
                service_id="svc-id",
                api_token="fxxx-test",
                client=client,
                purge_all_service_domains=True,
            )
            await p.purge(["https://repo.example.com/a"])
            await p.purge(["https://repo.example.com/b"])

        # The two discovery calls happen once each, not per purge.
        assert details.call_count == 1
        assert domains.call_count == 1

    @respx.mock
    async def test_discovery_401_is_terminal(self) -> None:
        respx.get("https://api.fastly.com/service/svc-id").mock(return_value=httpx.Response(401))
        # No purge calls should be made.
        purge_route = respx.post(url__regex=r"https://api\.fastly\.com/purge/.*").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        async with httpx.AsyncClient() as client:
            p = FastlyProvider(
                service_id="svc-id",
                api_token="fxxx-test",
                client=client,
                purge_all_service_domains=True,
            )
            result = await p.purge(["https://repo.example.com/a"])
        assert result.ok is False
        assert result.retryable is False
        assert "discovery" in result.message.lower()
        assert purge_route.call_count == 0

    @respx.mock
    async def test_discovery_503_is_retryable(self) -> None:
        respx.get("https://api.fastly.com/service/svc-id").mock(return_value=httpx.Response(503))
        async with httpx.AsyncClient() as client:
            p = FastlyProvider(
                service_id="svc-id",
                api_token="fxxx-test",
                client=client,
                purge_all_service_domains=True,
            )
            result = await p.purge(["https://repo.example.com/a"])
        assert result.ok is False
        assert result.retryable is True

    @respx.mock
    async def test_no_active_version_is_terminal(self) -> None:
        respx.get("https://api.fastly.com/service/svc-id").mock(
            return_value=httpx.Response(
                200,
                json={"id": "svc-id", "versions": [{"number": 1, "active": False}]},
            )
        )
        async with httpx.AsyncClient() as client:
            p = FastlyProvider(
                service_id="svc-id",
                api_token="fxxx-test",
                client=client,
                purge_all_service_domains=True,
            )
            result = await p.purge(["https://repo.example.com/a"])
        assert result.ok is False
        assert result.retryable is False
        assert "no active version" in result.message.lower()

    @respx.mock
    async def test_flag_off_skips_discovery(self) -> None:
        # No discovery routes registered; if the provider tried to call
        # them respx would raise. This confirms discovery is fully gated.
        purge_route = respx.post("https://api.fastly.com/purge/repo.example.com/a").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        async with httpx.AsyncClient() as client:
            p = FastlyProvider(
                service_id="svc-id",
                api_token="fxxx-test",
                client=client,
                # purge_all_service_domains defaults to False
            )
            result = await p.purge(["https://repo.example.com/a"])
        assert result.ok is True
        assert purge_route.call_count == 1


# ---------------------------------------------------------------------------
# CloudFront
# ---------------------------------------------------------------------------


class TestCloudFrontProvider:
    async def test_urls_converted_to_paths(self) -> None:
        with patch("mirror_mirage.providers.cloudfront.boto3") as mock_boto:
            client = MagicMock()
            mock_boto.client.return_value = client
            client.create_invalidation.return_value = {
                "Invalidation": {"Id": "I123", "Status": "InProgress"}
            }
            p = CloudFrontProvider(
                distribution_id="E1",
                region="us-east-1",
                access_key_id="AKIA",
                secret_access_key="x",
            )
            result = await p.purge(
                [
                    "https://cdn-us.almalinux.org/almalinux/9/os/x86_64/repodata/repomd.xml",
                    "https://cdn-us.almalinux.org/almalinux/9/os/x86_64/Packages/x.rpm",
                ]
            )
        assert result.ok is True
        call = client.create_invalidation.call_args
        batch = call.kwargs["InvalidationBatch"]
        paths = batch["Paths"]["Items"]
        assert "/almalinux/9/os/x86_64/repodata/repomd.xml" in paths
        assert "/almalinux/9/os/x86_64/Packages/x.rpm" in paths

    async def test_throttling_is_retryable(self) -> None:
        from botocore.exceptions import ClientError

        err = ClientError(
            {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}},
            "CreateInvalidation",
        )
        with patch("mirror_mirage.providers.cloudfront.boto3") as mock_boto:
            client = MagicMock()
            mock_boto.client.return_value = client
            client.create_invalidation.side_effect = err
            p = CloudFrontProvider(
                distribution_id="E1",
                region="us-east-1",
                access_key_id="x",
                secret_access_key="x",
            )
            result = await p.purge(["https://cdn.example.com/a"])
        assert result.ok is False
        assert result.retryable is True

    async def test_access_denied_is_terminal(self) -> None:
        from botocore.exceptions import ClientError

        err = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "nope"}},
            "CreateInvalidation",
        )
        with patch("mirror_mirage.providers.cloudfront.boto3") as mock_boto:
            client = MagicMock()
            mock_boto.client.return_value = client
            client.create_invalidation.side_effect = err
            p = CloudFrontProvider(
                distribution_id="E1",
                region="us-east-1",
                access_key_id="x",
                secret_access_key="x",
            )
            result = await p.purge(["https://cdn.example.com/a"])
        assert result.ok is False
        assert result.retryable is False

    async def test_no_such_distribution_is_terminal(self) -> None:
        from botocore.exceptions import ClientError

        err = ClientError(
            {"Error": {"Code": "NoSuchDistribution", "Message": "not found"}},
            "CreateInvalidation",
        )
        with patch("mirror_mirage.providers.cloudfront.boto3") as mock_boto:
            client = MagicMock()
            mock_boto.client.return_value = client
            client.create_invalidation.side_effect = err
            p = CloudFrontProvider(
                distribution_id="E1",
                region="us-east-1",
                access_key_id="x",
                secret_access_key="x",
            )
            result = await p.purge(["https://cdn.example.com/a"])
        assert result.retryable is False

    async def test_missing_credentials_is_terminal(self) -> None:
        # boto3 raises NoCredentialsError when its default credential chain
        # finds nothing. That can't be fixed by retrying.
        from botocore.exceptions import NoCredentialsError

        with patch("mirror_mirage.providers.cloudfront.boto3") as mock_boto:
            client = MagicMock()
            mock_boto.client.return_value = client
            client.create_invalidation.side_effect = NoCredentialsError()
            p = CloudFrontProvider(
                distribution_id="E1",
                region="us-east-1",
                access_key_id=None,
                secret_access_key=None,
            )
            result = await p.purge(["https://cdn.example.com/a"])
        assert result.ok is False
        assert result.retryable is False
        assert "auth" in result.message.lower()

    async def test_partial_credentials_is_terminal(self) -> None:
        # Half a credential pair (e.g. only AWS_ACCESS_KEY_ID in env) is
        # also terminal — operator needs to fix the config.
        from botocore.exceptions import PartialCredentialsError

        with patch("mirror_mirage.providers.cloudfront.boto3") as mock_boto:
            client = MagicMock()
            mock_boto.client.return_value = client
            client.create_invalidation.side_effect = PartialCredentialsError(
                provider="env", cred_var="AWS_SECRET_ACCESS_KEY"
            )
            p = CloudFrontProvider(
                distribution_id="E1",
                region="us-east-1",
                access_key_id="AKIA",
                secret_access_key=None,
            )
            result = await p.purge(["https://cdn.example.com/a"])
        assert result.ok is False
        assert result.retryable is False


# ---------------------------------------------------------------------------
# GCP Cloud CDN
# ---------------------------------------------------------------------------


@pytest.fixture
def gcp_sa_file(tmp_path: Path) -> Path:
    """A bogus service-account JSON file. Tests patch the token-mint step."""
    sa = tmp_path / "gcp-sa.json"
    sa.write_text(
        '{"type": "service_account", "project_id": "alma-prod",'
        ' "private_key_id": "x", "private_key": "x", "client_email": "x@x.iam"}'
    )
    return sa


class TestGcpCdnProvider:
    @respx.mock
    async def test_one_call_per_url(self, gcp_sa_file: Path) -> None:
        route = respx.post(
            "https://compute.googleapis.com/compute/v1/projects/alma-prod"
            "/global/urlMaps/alma-prod-lb/invalidateCache"
        ).mock(return_value=httpx.Response(200, json={"status": "DONE"}))
        with patch("mirror_mirage.providers.gcp.service_account") as mock_sa:
            creds = MagicMock()
            creds.token = "tok"
            creds.expired = False
            mock_sa.Credentials.from_service_account_file.return_value = creds
            async with httpx.AsyncClient() as client:
                p = GcpCdnProvider(
                    project="alma-prod",
                    url_map="alma-prod-lb",
                    service_account_file=gcp_sa_file,
                    client=client,
                )
                result = await p.purge(
                    [
                        "https://cdn-eu.almalinux.org/almalinux/9/x.rpm",
                        "https://cdn-eu.almalinux.org/almalinux/9/y.rpm",
                        "https://cdn-eu.almalinux.org/almalinux/9/z.rpm",
                    ]
                )
        assert result.ok is True
        assert route.call_count == 3

    @respx.mock
    async def test_request_body_has_path_and_host(self, gcp_sa_file: Path) -> None:
        route = respx.post(
            "https://compute.googleapis.com/compute/v1/projects/alma-prod"
            "/global/urlMaps/alma-prod-lb/invalidateCache"
        ).mock(return_value=httpx.Response(200, json={"status": "DONE"}))
        with patch("mirror_mirage.providers.gcp.service_account") as mock_sa:
            creds = MagicMock()
            creds.token = "tok"
            creds.expired = False
            mock_sa.Credentials.from_service_account_file.return_value = creds
            async with httpx.AsyncClient() as client:
                p = GcpCdnProvider(
                    project="alma-prod",
                    url_map="alma-prod-lb",
                    service_account_file=gcp_sa_file,
                    client=client,
                )
                await p.purge(["https://cdn-eu.almalinux.org/almalinux/9/os/x86_64/x.rpm"])
        body = route.calls[0].request.read()
        import json

        payload = json.loads(body)
        assert payload["host"] == "cdn-eu.almalinux.org"
        assert payload["path"] == "/almalinux/9/os/x86_64/x.rpm"

    @respx.mock
    async def test_429_is_retryable(self, gcp_sa_file: Path) -> None:
        respx.post(
            "https://compute.googleapis.com/compute/v1/projects/alma-prod"
            "/global/urlMaps/alma-prod-lb/invalidateCache"
        ).mock(return_value=httpx.Response(429))
        with patch("mirror_mirage.providers.gcp.service_account") as mock_sa:
            creds = MagicMock()
            creds.token = "tok"
            creds.expired = False
            mock_sa.Credentials.from_service_account_file.return_value = creds
            async with httpx.AsyncClient() as client:
                p = GcpCdnProvider(
                    project="alma-prod",
                    url_map="alma-prod-lb",
                    service_account_file=gcp_sa_file,
                    client=client,
                )
                result = await p.purge(["https://cdn-eu.almalinux.org/almalinux/9/x.rpm"])
        assert result.ok is False
        assert result.retryable is True

    @respx.mock
    async def test_quota_paces_calls(self, gcp_sa_file: Path) -> None:
        """With quota_per_minute=N, the (N+1)-th call must wait for the window.

        We set the quota low and time how long N+1 calls take. The (N+1)-th
        call should not return until the rate limiter has space; we monkey-
        patch the limiter's "minute" to a sub-second window so the test
        stays fast.
        """
        import asyncio

        respx.post(
            "https://compute.googleapis.com/compute/v1/projects/alma-prod"
            "/global/urlMaps/alma-prod-lb/invalidateCache"
        ).mock(return_value=httpx.Response(200, json={"status": "DONE"}))
        with patch("mirror_mirage.providers.gcp.service_account") as mock_sa:
            creds = MagicMock()
            creds.token = "tok"
            creds.expired = False
            mock_sa.Credentials.from_service_account_file.return_value = creds
            async with httpx.AsyncClient() as client:
                # The provider exposes the sliding-window length as
                # ``_quota_window_seconds`` for test injection. Production
                # uses 60.0.
                p = GcpCdnProvider(
                    project="alma-prod",
                    url_map="alma-prod-lb",
                    service_account_file=gcp_sa_file,
                    client=client,
                    quota_per_minute=3,
                )
                p._quota_window_seconds = 0.5  # type: ignore[attr-defined]

                urls = [f"https://cdn.example/{i}" for i in range(4)]
                start = asyncio.get_event_loop().time()
                result = await p.purge(urls)
                elapsed = asyncio.get_event_loop().time() - start

        assert result.ok is True
        # First 3 should pace through immediately; the 4th must wait at
        # least one window expiry. We allow generous tolerance for jitter.
        assert elapsed >= 0.4, f"expected pacing to delay 4th call by ~window; elapsed={elapsed}"

    @respx.mock
    async def test_under_quota_does_not_pace(self, gcp_sa_file: Path) -> None:
        """With usage below quota, no artificial delay is introduced."""
        import asyncio

        respx.post(
            "https://compute.googleapis.com/compute/v1/projects/alma-prod"
            "/global/urlMaps/alma-prod-lb/invalidateCache"
        ).mock(return_value=httpx.Response(200, json={"status": "DONE"}))
        with patch("mirror_mirage.providers.gcp.service_account") as mock_sa:
            creds = MagicMock()
            creds.token = "tok"
            creds.expired = False
            mock_sa.Credentials.from_service_account_file.return_value = creds
            async with httpx.AsyncClient() as client:
                p = GcpCdnProvider(
                    project="alma-prod",
                    url_map="alma-prod-lb",
                    service_account_file=gcp_sa_file,
                    client=client,
                    quota_per_minute=500,
                )
                p._quota_window_seconds = 60.0  # type: ignore[attr-defined]
                start = asyncio.get_event_loop().time()
                await p.purge([f"https://cdn.example/{i}" for i in range(5)])
                elapsed = asyncio.get_event_loop().time() - start
        # 5 calls well under a 500/min quota — should be near-instant.
        assert elapsed < 0.5

    @respx.mock
    async def test_403_is_terminal(self, gcp_sa_file: Path) -> None:
        respx.post(
            "https://compute.googleapis.com/compute/v1/projects/alma-prod"
            "/global/urlMaps/alma-prod-lb/invalidateCache"
        ).mock(return_value=httpx.Response(403))
        with patch("mirror_mirage.providers.gcp.service_account") as mock_sa:
            creds = MagicMock()
            creds.token = "tok"
            creds.expired = False
            mock_sa.Credentials.from_service_account_file.return_value = creds
            async with httpx.AsyncClient() as client:
                p = GcpCdnProvider(
                    project="alma-prod",
                    url_map="alma-prod-lb",
                    service_account_file=gcp_sa_file,
                    client=client,
                )
                result = await p.purge(["https://cdn-eu.almalinux.org/almalinux/9/x.rpm"])
        assert result.ok is False
        assert result.retryable is False


# ---------------------------------------------------------------------------
# Dry-run provider
# ---------------------------------------------------------------------------


class TestDryRunProvider:
    """The dry-run provider must log every batch but never make an HTTP call."""

    @respx.mock
    async def test_purge_returns_success_without_calling_any_api(self) -> None:
        # No respx routes registered — any HTTP call would error.
        p = DryRunProvider(cdn_name="fastly_main", wrapped_type="fastly")
        result = await p.purge(["https://x.example/a", "https://x.example/b"])
        assert result.ok is True
        assert result.retryable is False
        assert "dry-run" in result.message.lower()

    async def test_purge_logs_each_url(self, caplog) -> None:
        import logging

        caplog.set_level(logging.INFO, logger="mirror_mirage")
        p = DryRunProvider(cdn_name="cloudfront_us", wrapped_type="cloudfront")
        await p.purge(["https://cdn.example/a", "https://cdn.example/b"])
        log_text = "\n".join(r.getMessage() for r in caplog.records)
        # Every URL should appear in the log output somewhere.
        assert "https://cdn.example/a" in log_text
        assert "https://cdn.example/b" in log_text
        # And the CDN name + wrapped type, so operators can attribute it.
        assert "cloudfront_us" in log_text
        assert "cloudfront" in log_text

    async def test_purge_handles_empty_batch(self) -> None:
        p = DryRunProvider(cdn_name="x", wrapped_type="fastly")
        result = await p.purge([])
        assert result.ok is True


class TestBuildProvidersDryRun:
    """build_providers(dry_run=True) must wrap every CDN with DryRunProvider."""

    def test_dry_run_wraps_every_cdn(self, basic_config) -> None:
        from mirror_mirage.providers import build_providers

        providers = build_providers(basic_config, dry_run=True)
        assert set(providers) == set(basic_config.cdns)
        for prov in providers.values():
            assert isinstance(prov, DryRunProvider)


# ---------------------------------------------------------------------------
# PurgeResult sanity
# ---------------------------------------------------------------------------


class TestPurgeResult:
    def test_success(self) -> None:
        r = PurgeResult(ok=True, retryable=False, message="done")
        assert r.ok and not r.retryable

    def test_retryable_failure(self) -> None:
        r = PurgeResult(ok=False, retryable=True, message="429")
        assert not r.ok and r.retryable
