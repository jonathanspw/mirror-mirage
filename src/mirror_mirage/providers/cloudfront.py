"""AWS CloudFront invalidation provider.

Uses boto3, which is synchronous; calls are dispatched via
:func:`asyncio.to_thread`. See ``docs/cdn-providers.md`` for API details
and error classification.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import urlparse

import boto3
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    NoCredentialsError,
    PartialCredentialsError,
)

from mirror_mirage.providers.base import PurgeResult

_RETRYABLE_CODES = frozenset(
    {
        "Throttling",
        "ThrottlingException",
        "RequestLimitExceeded",
        "TooManyRequestsException",
        "ServiceUnavailable",
        "ServiceUnavailableException",
        "TooManyInvalidationsInProgress",
        "InternalFailure",
        "InternalServerError",
    }
)
_log = logging.getLogger("mirror_mirage.cloudfront")


class CloudFrontProvider:
    """Purges paths via the CloudFront ``create_invalidation`` API."""

    def __init__(
        self,
        *,
        distribution_id: str,
        region: str = "us-east-1",
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
    ) -> None:
        """Create a provider for a single CloudFront distribution.

        ``access_key_id`` and ``secret_access_key`` may be ``None`` — in
        which case boto3 will fall back to its default credential chain
        (instance role, env vars, etc.).
        """
        self._distribution_id = distribution_id
        self._region = region
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            kwargs: dict[str, Any] = {"region_name": self._region}
            if self._access_key_id is not None:
                kwargs["aws_access_key_id"] = self._access_key_id
                kwargs["aws_secret_access_key"] = self._secret_access_key
            self._client = boto3.client("cloudfront", **kwargs)
        return self._client

    async def purge(self, urls: list[str]) -> PurgeResult:
        """Submit one invalidation containing every URL's path.

        URLs are converted to paths (the scheme/host are stripped) before
        being sent to CloudFront. The boto3 call runs inside
        :func:`asyncio.to_thread` so the event loop is not blocked.
        """
        if not urls:
            return PurgeResult(ok=True, retryable=False, message="empty")

        # Deduplicate paths; CloudFront bills per path so dedup matters.
        paths = sorted({urlparse(u).path or "/" for u in urls})
        caller_ref = f"mirror-mirage-{time.time_ns()}"

        def _call() -> Any:
            client = self._get_client()
            return client.create_invalidation(
                DistributionId=self._distribution_id,
                InvalidationBatch={
                    "Paths": {"Quantity": len(paths), "Items": paths},
                    "CallerReference": caller_ref,
                },
            )

        try:
            await asyncio.to_thread(_call)
        except (NoCredentialsError, PartialCredentialsError) as e:
            # Missing or half-set AWS credentials won't fix themselves —
            # operator must update secrets.yaml (or the instance profile
            # / ~/.aws/credentials, depending on how creds are sourced).
            return PurgeResult(ok=False, retryable=False, message=f"auth: {e}")
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            retryable = code in _RETRYABLE_CODES
            return PurgeResult(
                ok=False,
                retryable=retryable,
                message=f"{code}: {e}",
            )
        except BotoCoreError as e:
            return PurgeResult(ok=False, retryable=True, message=f"botocore: {e}")
        except Exception as e:
            return PurgeResult(ok=False, retryable=True, message=f"transport: {e}")

        return PurgeResult(ok=True, retryable=False, message="ok")
