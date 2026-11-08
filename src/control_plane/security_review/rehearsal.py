"""Run two disposable SeaweedFS instances. No deployed endpoint or credential is accepted."""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable
from contextlib import ExitStack, closing
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from control_plane.deployment.provisioner.handoff import RECEIPTS_BUCKET
from control_plane.deployment.provisioner.onprem_bundle import (
    APP_OBJECT_ACTIONS,
    MIGRATION_OBJECT_ACTIONS,
)

BUCKET = RECEIPTS_BUCKET
OTHER = "review-unrelated"
KEY = "receipts/security-review.json"


def _client(port: int, identity: dict[str, Any]) -> Any:
    credential = identity["credentials"][0]
    return boto3.client(
        "s3",
        endpoint_url=f"http://127.0.0.1:{port}",
        aws_access_key_id=credential["accessKey"],
        aws_secret_access_key=credential["secretKey"],
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            connect_timeout=2,
            read_timeout=8,
            proxies={},
            retries={"mode": "standard", "total_max_attempts": 2},
            s3={"addressing_style": "path"},
        ),
    )


def _roundtrip(client: Any, key: str, *, copy: bool = False) -> str:
    payload = secrets.token_bytes(24)
    client.put_object(Bucket=BUCKET, Key=key, Body=payload)
    if copy:
        client.copy_object(
            Bucket=BUCKET, Key=key + ".copy", CopySource={"Bucket": BUCKET, "Key": key}
        )
        key += ".copy"
    body = client.get_object(Bucket=BUCKET, Key=key)["Body"]
    try:
        return "allowed" if body.read() == payload else "unknown"
    finally:
        body.close()


def _trial(binary: str, actions: list[str]) -> dict[str, str]:
    def identity(name: str, grants: list[str]) -> dict[str, Any]:
        return {
            "name": name,
            "actions": grants,
            "credentials": [
                {"accessKey": secrets.token_hex(12), "secretKey": secrets.token_hex(24)}
            ],
        }

    admin = identity("object-migration", list(MIGRATION_OBJECT_ACTIONS))
    app = identity("app", actions)
    # Reserve distinct ephemeral ports, then release immediately before child startup.
    flags = [
        "master.port",
        "master.port.grpc",
        "volume.port",
        "volume.port.grpc",
        "filer.port",
        "filer.port.grpc",
        "s3.port",
        "s3.port.grpc",
    ]
    with tempfile.TemporaryDirectory(prefix="foxtrot-storage-review-") as directory:
        config = Path(directory) / "s3.json"
        config.write_text(json.dumps({"identities": [admin, app]}), encoding="utf-8")
        with ExitStack() as sockets:
            ports = {}
            for flag in flags:
                sock = sockets.enter_context(socket.socket())
                sock.bind(("127.0.0.1", 0))
                ports[flag] = sock.getsockname()[1]
        args = [
            binary,
            "server",
            "-dir=.",
            "-ip=127.0.0.1",
            "-ip.bind=127.0.0.1",
            "-master.telemetry=false",
            "-master.volumeSizeLimitMB=8",
            "-volume.max=8",
            "-filer",
            "-s3",
            "-s3.config=s3.json",
            "-s3.port.iceberg=0",
            "-s3.port.lance=0",
            *[f"-{flag}={port}" for flag, port in ports.items()],
        ]
        proc = subprocess.Popen(
            args,
            cwd=directory,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env={
                k: v
                for k, v in os.environ.items()
                if k.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TMP", "TEMP", "LANG"}
            }
            | {"HOME": directory, "USERPROFILE": directory},
        )
        try:
            with (
                closing(_client(ports["s3.port"], admin)) as migration,
                closing(_client(ports["s3.port"], app)) as actor,
            ):
                deadline = time.monotonic() + 24
                while True:
                    if proc.poll() is not None:
                        raise RuntimeError("Sandbox process exited")
                    try:
                        migration.create_bucket(Bucket=BUCKET)
                        break
                    except (
                        ClientError,
                        EndpointConnectionError,
                        ConnectTimeoutError,
                        ReadTimeoutError,
                    ):
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.3)
                migration.create_bucket(Bucket=OTHER)

                def result(fn: Callable[[], str]) -> str:
                    try:
                        return fn()
                    except ClientError as exc:
                        return (
                            "denied"
                            if exc.response["Error"]["Code"] == "AccessDenied"
                            else "unknown"
                        )

                def list_other() -> str:
                    actor.list_objects_v2(Bucket=OTHER, MaxKeys=1)
                    return "allowed"

                return {
                    "receipt": result(lambda: _roundtrip(actor, KEY)),
                    "otherBucket": result(list_other),
                    "copy": result(lambda: _roundtrip(migration, "copy/probe.json", copy=True)),
                }
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def _candidate(
    before: dict[str, str], model: str | None, attempts: list[dict[str, Any]]
) -> list[str]:
    if not model:
        return list(APP_OBJECT_ACTIONS)
    # The model can only select which scoped app actions to test. Never run generated commands.
    client = boto3.client(
        "bedrock-runtime",
        config=Config(connect_timeout=3, read_timeout=15, retries={"total_max_attempts": 1}),
    )
    reply = client.converse(
        modelId=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "text": "Select app actions on receipts only. Preserve receipt writes, "
                        "reads and listing. Keep object-migration intact. Reply only with a JSON "
                        "array drawn from Read, Write, List. "
                        f"Sandbox baseline: {json.dumps(before)}. "
                        f"Previous trials: {json.dumps(attempts)}"
                    }
                ],
            }
        ],
        inferenceConfig={"maxTokens": 64, "temperature": 0},
    )
    selected = json.loads(reply["output"]["message"]["content"][0]["text"])
    if (
        not isinstance(selected, list)
        or not selected
        or not all(isinstance(x, str) and x in APP_OBJECT_ACTIONS for x in selected)
    ):
        raise ValueError("Invalid proposed actions")
    return list(dict.fromkeys(selected))


def run_rehearsal(binary: str, model: str | None) -> dict[str, Any]:
    before = _trial(binary, list(APP_OBJECT_ACTIONS))
    attempts: list[dict[str, Any]] = []
    for _ in range(2 if model else 1):
        chosen = _candidate(before, model, attempts)
        actions = [f"{action}:{BUCKET}" for action in chosen]
        after = _trial(binary, actions)
        passed = (
            before == dict(receipt="allowed", otherBucket="allowed", copy="allowed")
            and after == dict(receipt="allowed", otherBucket="denied", copy="allowed")
            and set(chosen) == set(APP_OBJECT_ACTIONS)
        )
        attempts.append({"actions": actions, "observations": after, "passed": passed})
        if passed:
            break
    return {
        "bucket": BUCKET,
        "extraBucket": OTHER,
        "before": before,
        "after": after,
        "beforeActions": list(APP_OBJECT_ACTIONS),
        "afterActions": actions,
        "method": "bedrock" if model else "rule",
        "attempts": attempts,
        "passed": passed,
    }
