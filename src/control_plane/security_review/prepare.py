"""Prepare/reset one empty, private demo bucket. Never modifies the application's bucket."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx

from .models import ReviewConfig
from .provider import CloudProvider


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", default="ap-northeast-2")
    parser.add_argument("--location", default="ASIA-NORTHEAST3")
    args = parser.parse_args()
    cfg = ReviewConfig.model_validate_json(Path(args.config).read_text())
    if cfg.read_only:
        parser.error("read-only profiles cannot prepare or reset a demo bucket")
    if cfg.mode != "cloud" or not cfg.extra_bucket.startswith("foxtrot-security-demo-"):
        parser.error("only a cloud profile with a foxtrot-security-demo- bucket is supported")
    provider = CloudProvider(cfg, args.region)
    headers = provider._headers()
    base = "https://storage.googleapis.com/storage/v1/b"
    label = {"purpose": "foxtrot-security-demo"}
    with httpx.Client(headers=headers, timeout=15, follow_redirects=False) as http:
        response = http.get(f"{base}/{cfg.extra_bucket}")
        if response.status_code == 404:
            response = http.post(
                base,
                params={"project": args.project},
                json={
                    "name": cfg.extra_bucket,
                    "location": args.location,
                    "labels": label,
                    "iamConfiguration": {
                        "uniformBucketLevelAccess": {"enabled": True},
                        "publicAccessPrevention": "enforced",
                    },
                },
            )
        response.raise_for_status()
        metadata = response.json()
        if metadata.get("labels", {}).get("purpose") != label["purpose"]:
            raise ValueError("not a designated security demo bucket")
        if metadata.get("iamConfiguration", {}).get("publicAccessPrevention") != "enforced":
            raise ValueError("the demo bucket must prevent public access")
        objects = http.get(
            f"{base}/{cfg.extra_bucket}/o", params={"maxResults": 1, "versions": "true"}
        )
        objects.raise_for_status()
        if objects.json().get("items"):
            raise ValueError("refusing to broaden access to a nonempty bucket")
        policy = provider._policy(cfg.extra_bucket, headers)
        if not policy.get("etag"):
            raise ValueError("missing policy etag")
        for binding in policy.setdefault("bindings", []):
            if binding["role"] == "roles/storage.objectUser" and not binding.get("condition"):
                if cfg.gcp_principal not in binding["members"]:
                    binding["members"].append(cfg.gcp_principal)
                break
        else:
            policy["bindings"].append(
                {"role": "roles/storage.objectUser", "members": [cfg.gcp_principal]}
            )
        provider.apply(policy)
    print(
        json.dumps(
            {
                "bucket": cfg.extra_bucket,
                "principal": cfg.gcp_principal,
                "state": "extra-access-ready",
                "applicationBucketChanged": False,
            }
        )
    )


if __name__ == "__main__":
    main()
