"""Start this checkout against its own persistent DynamoDB Local container."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import boto3
import yaml
from botocore.config import Config
from botocore.exceptions import EndpointConnectionError

ROOT = Path(__file__).resolve().parents[1]
NAME = "foxtrot-platform-dynamodb"


def main() -> None:
    os.chdir(ROOT)
    existing = subprocess.run(["docker", "inspect", NAME], capture_output=True, text=True)
    if existing.returncode:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                NAME,
                "--user",
                "0:0",
                "--label",
                "foxtrot.component=platform-local-dynamodb",
                "-p",
                "127.0.0.1:18005:8000",
                "-v",
                "foxtrot-platform-dynamodb:/home/dynamodblocal/data",
                "amazon/dynamodb-local:latest",
                "-jar",
                "DynamoDBLocal.jar",
                "-sharedDb",
                "-dbPath",
                "/home/dynamodblocal/data",
            ],
            check=True,
        )
    else:
        container = json.loads(existing.stdout)[0]
        if container["Config"]["Image"] != "amazon/dynamodb-local:latest":
            raise RuntimeError(f"{NAME} belongs to another image; it was left unchanged")
        if not container["State"]["Running"]:
            subprocess.run(["docker", "start", NAME], check=True)
    client = boto3.client(
        "dynamodb",
        endpoint_url="http://127.0.0.1:18005",
        region_name="ap-northeast-2",
        aws_access_key_id="local",
        aws_secret_access_key="local",
        config=Config(connect_timeout=1, read_timeout=3, retries={"total_max_attempts": 1}),
    )
    for attempt in range(30):
        try:
            client.list_tables()
            break
        except EndpointConnectionError:
            if attempt == 29:
                raise
            time.sleep(1)
    table = "foxtrot-platform-local"
    try:
        client.describe_table(TableName=table)
    except client.exceptions.ResourceNotFoundException:
        client.create_table(
            TableName=table,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": key, "AttributeType": "S"} for key in ("pk", "sk")
            ],
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
        )
    runtime = ROOT / "runtime"
    runtime.mkdir(exist_ok=True)
    seed = yaml.safe_load((ROOT / "config/seed.example.yaml").read_text())
    seed["environments"]["aws"]["appUrl"] = "http://127.0.0.1:18081"
    seed["environments"]["gcp"]["appUrl"] = "http://127.0.0.1:18082"
    (runtime / "seed.yaml").write_text(yaml.safe_dump(seed, allow_unicode=True, sort_keys=False))
    values = {
        "ENVIRONMENT": "local",
        "HOST": "127.0.0.1",
        "PORT": "8000",
        "SEED_PATH": str(runtime / "seed.yaml"),
        "TABLE_NAME": table,
        "DYNAMODB_ENDPOINT": "http://127.0.0.1:18005",
        "AUTH_DISABLED": "true",
        "LEGACY_SEED_ENABLED": "false",
        "RUNTIME_DOCKER_ENABLED": "true",
        "RUNTIME_TARGETS": json.dumps(
            {"local:developer/gcp": {"provider": "docker", "hostPort": 18082}}
        ),
    }
    os.environ.update({f"CONTROL_PLANE_{key}": value for key, value in values.items()})
    os.environ.update({"AWS_ACCESS_KEY_ID": "local", "AWS_SECRET_ACCESS_KEY": "local"})
    for name in (
        "DB_ADAPTER_TOKEN",
        "REDIS_ADAPTER_TOKEN",
        "OBJECT_ADAPTER_TOKEN",
        "TRAFFIC_SERVICE_TOKEN",
        "WORKLOAD_TOKEN",
    ):
        os.environ[name] = "local"
    from control_plane.main import run

    run()


if __name__ == "__main__":
    main()
