"""Read a synthetic receipt and list one demo bucket under the actual designated identities."""

from __future__ import annotations

import json
from functools import partial
from typing import Any
from urllib.parse import quote

import httpx
from google.auth.transport.requests import Request
from google.oauth2.service_account import Credentials

from control_plane.config import resolve_secret

from .models import ReviewConfig

CANARY = "receipts/security-review-canary.json"


def _observe(client: httpx.Client, url: str, token: str, *, listing: bool) -> str:
    headers = {"Authorization": f"Bearer {token}"}
    if not listing:
        headers["Range"] = "bytes=0-0"
    with client.stream(
        "GET",
        url,
        headers=headers,
        params={"maxResults": 1, "fields": "kind"} if listing else {"alt": "media"},
    ) as r:
        if r.status_code in {200, 206} and (listing or next(r.iter_bytes(chunk_size=1), b"")):
            return "allowed"
        if listing and r.status_code == 403:
            r.read()
            message = r.json().get("error", {}).get("message", "")
            if "storage.objects.list" in message and "denied" in message.lower():
                return "denied"
        return "unknown"


def observe(config: ReviewConfig, region: str) -> dict[str, str]:
    accounts = config.probes
    assert accounts is not None
    result = dict(receipt="unknown", otherBucket="unknown", copy="unknown")
    root = "https://storage.googleapis.com/storage/v1/b/"
    canary = f"{root}{config.gcp_bucket}/o/{quote(CANARY, safe='')}"
    for name, ref, email in [
        ("receipt", accounts.app_secret_ref, config.gcp_principal.removeprefix("serviceAccount:")),
        ("copy", accounts.copy_secret_ref, accounts.copy_email),
    ]:
        try:
            info = json.loads(resolve_secret(ref, region=region))
            if info["client_email"] != email:
                continue
            factory: Any = Credentials.from_service_account_info
            credentials = factory(
                info, scopes=["https://www.googleapis.com/auth/devstorage.read_only"]
            )
            credentials.refresh(partial(Request(), timeout=5))
            with httpx.Client(timeout=5, follow_redirects=False) as client:
                result[name] = _observe(client, canary, credentials.token, listing=False)
                if name == "receipt":
                    result["otherBucket"] = _observe(
                        client, f"{root}{config.extra_bucket}/o", credentials.token, listing=True
                    )
        except Exception:
            # Do not persist provider messages, tokens, or object contents.
            continue
    return result
