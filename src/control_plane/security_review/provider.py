"""Cloud reads and one allowlisted bucket IAM write. Credentials never enter review records."""

from __future__ import annotations

import json
from copy import deepcopy
from functools import partial
from typing import Any, Protocol
from urllib.parse import quote, unquote

import boto3
import httpx
from botocore.config import Config

from control_plane.config import resolve_secret

from .models import ReviewConfig


class Provider(Protocol):
    def collect(self) -> dict[str, Any]: ...

    def apply(self, policy: dict[str, Any]) -> None: ...


class CloudProvider:
    def __init__(self, config: ReviewConfig, region: str) -> None:
        self.config, self.region = config, region

    def _headers(self) -> dict[str, str]:
        import google.auth
        from google.auth.transport.requests import Request
        from google.oauth2.service_account import Credentials

        scopes = ["https://www.googleapis.com/auth/devstorage.full_control"]
        reference = self.config.gcp_credentials_ref
        if reference:
            value = (
                boto3.client(
                    "secretsmanager",
                    region_name=self.region,
                    config=Config(
                        connect_timeout=3, read_timeout=10, retries={"total_max_attempts": 1}
                    ),
                ).get_secret_value(SecretId=reference.removeprefix("aws:"))["SecretString"]
                if reference.startswith("aws:")
                else resolve_secret(reference, region=self.region)
            )
            factory: Any = Credentials.from_service_account_info
            credentials: Any = factory(json.loads(value), scopes=scopes)
        else:
            credentials, _ = google.auth.default(scopes=scopes)
        credentials.refresh(partial(Request(), timeout=10))
        return {"Authorization": f"Bearer {credentials.token}"}

    def _policy(self, bucket: str, headers: dict[str, str]) -> dict[str, Any]:
        response = httpx.get(
            f"https://storage.googleapis.com/storage/v1/b/{quote(bucket, safe='')}/iam",
            params={"optionsRequestedPolicyVersion": 3},
            headers=headers,
            timeout=10,
            follow_redirects=False,
        )
        response.raise_for_status()
        return dict(response.json())

    def collect(self) -> dict[str, Any]:
        cfg = self.config
        iam = boto3.client(
            "iam",
            region_name=self.region,
            config=Config(connect_timeout=3, read_timeout=10, retries={"total_max_attempts": 1}),
        )
        document = iam.get_role_policy(RoleName=cfg.aws_role, PolicyName=cfg.aws_policy)[
            "PolicyDocument"
        ]
        if isinstance(document, str):
            document = json.loads(unquote(document))
        headers = self._headers()
        return {
            "source": document,
            "target": self._policy(cfg.gcp_bucket, headers),
            "extra": self._policy(cfg.extra_bucket, headers),
        }

    def apply(self, policy: dict[str, Any]) -> None:
        # Bucket and principal never come from the POST body. etag prevents lost updates.
        response = httpx.put(
            "https://storage.googleapis.com/storage/v1/b/"
            f"{quote(self.config.extra_bucket, safe='')}/iam",
            headers=self._headers(),
            json=policy,
            timeout=10,
            follow_redirects=False,
        )
        response.raise_for_status()


class FixtureProvider:
    """Explicit local integration fixture. Uses the same evaluator, API and persistence path."""

    def __init__(self, config: ReviewConfig) -> None:
        self.writes = 0
        self.data: dict[str, Any] = {
            "source": {
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
                        "Resource": f"arn:aws:s3:::{config.aws_bucket}",
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                        "Resource": f"arn:aws:s3:::{config.aws_bucket}/*",
                    },
                ]
            },
            "target": {
                "etag": "target-1",
                "version": 3,
                "bindings": [
                    {"role": "roles/storage.objectUser", "members": [config.gcp_principal]},
                ],
            },
            "extra": {
                "etag": "extra-1",
                "version": 3,
                "bindings": [
                    {
                        "role": "roles/storage.objectUser",
                        "members": [
                            config.gcp_principal,
                            "serviceAccount:other@demo-project.iam.gserviceaccount.com",
                        ],
                    },
                ],
            },
        }

    def collect(self) -> dict[str, Any]:
        return deepcopy(self.data)

    def apply(self, policy: dict[str, Any]) -> None:
        if policy["etag"] != self.data["extra"]["etag"]:
            raise ValueError("stale policy")
        self.writes += 1
        self.data["extra"] = deepcopy(policy)
        self.data["extra"]["etag"] = f"extra-{self.writes + 1}"
