"""Check (default) or regenerate the Foxtrot demo ACL. Never writes to AWS or Tailscale."""

import argparse
import ipaddress
import json
import socket
import subprocess
from pathlib import Path

POLICY = Path(__file__).with_name("foxtrot-onprem-acl.json")
SERVICES = (
    "control-plane",
    "db-proxy",
    "object-migration",
    "redis-proxy",
    "redis-migrator",
    "redis-syncworker",
)
TAG = "tag:onprem"
RDS = "chiikawa-aws-3e3d-db.cxg4inxrw1qh.ap-northeast-2.rds.amazonaws.com"


def aws(profile, *args):
    return json.loads(
        subprocess.check_output(
            ["aws", *args, "--region", "ap-northeast-2", "--profile", profile, "--output", "json"],
            text=True,
        )
    )


def current_tasks(profile):
    if aws(profile, "sts", "get-caller-identity")["Account"] != "952561150367":
        raise SystemExit("Wrong AWS account")

    def ecs(op, *args):
        return aws(profile, "ecs", op, "--cluster", "foxtrot-platform", *args)

    result = {}
    services = ecs("describe-services", "--services", *SERVICES)["services"]
    for svc in services:
        name = svc["serviceName"]
        if len(svc["deployments"]) != 1 or svc["deployments"][0]["rolloutState"] != "COMPLETED":
            raise SystemExit(f"{name}: wait for a stable deployment")
        ids = ecs("list-tasks", "--service-name", name)["taskArns"]
        if not ids or len(ids) != svc["desiredCount"]:
            raise SystemExit(f"{name}: unexpected task count")
        tasks = ecs("describe-tasks", "--tasks", *ids)["tasks"]
        ips = {
            d["value"]
            for t in tasks
            if t["lastStatus"] == "RUNNING" and t["group"] == f"service:{name}"
            for a in t["attachments"]
            for d in a["details"]
            if d["name"] == "privateIPv4Address"
        }
        if len(ips) != len(ids) or not all(
            ipaddress.ip_address(ip) in ipaddress.ip_network("10.30.0.0/16") for ip in ips
        ):
            raise SystemExit(f"{name}: unexpected task addresses")
        result[name] = sorted(ips)
    if set(result) != set(SERVICES):
        raise SystemExit("Missing ECS services")
    return result


def policy(tasks, rds, mgmt_gateway, aws_gateway):
    def tcp(src, dst):
        return {"action": "accept", "proto": "tcp", "src": src, "dst": dst}

    ports = {"control-plane": 8000, "db-proxy": 6432, "redis-proxy": 6379, "object-migration": 8080}
    return {
        "tagOwners": {TAG: ["autogroup:admin"]},
        "acls": [
            {"action": "accept", "src": [TAG], "dst": ["10.30.0.2:53"]},
            tcp([TAG], [f"{ip}:{port}" for name, port in ports.items() for ip in tasks[name]]),
            tcp([mgmt_gateway], [f"{TAG}:22"]),
            tcp(tasks["control-plane"], [f"{TAG}:22,80"]),
            tcp([TAG], [f"{rds}:5432"]),
            tcp(tasks["db-proxy"], ["100.115.99.73:5432"]),
            tcp(
                sorted(tasks["redis-proxy"] + tasks["redis-syncworker"] + tasks["redis-migrator"]),
                [f"{TAG}:6379"],
            ),
            tcp(tasks["object-migration"], [f"{TAG}:8333,18888"]),
            tcp([aws_gateway], [f"{TAG}:8333"]),
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="foxtrot")
    parser.add_argument(
        "--mgmt-gateway", default="100.99.0.85", help="Confirmed management router Tailnet IP"
    )
    parser.add_argument(
        "--aws-gateway", default="100.83.2.70", help="Confirmed workload router Tailnet IP"
    )
    parser.add_argument(
        "--write", action="store_true", help="Update only the local JSON for review"
    )
    args = parser.parse_args()
    for address in (args.mgmt_gateway, args.aws_gateway):
        if ipaddress.ip_address(address) not in ipaddress.ip_network("100.64.0.0/10"):
            raise SystemExit("Expected router Tailnet IP")
    (rds,) = sorted({r[4][0] for r in socket.getaddrinfo(RDS, 5432, socket.AF_INET)})
    if ipaddress.ip_address(rds) not in ipaddress.ip_network("10.10.0.0/16"):
        raise SystemExit("Unexpected RDS address")
    expected = policy(current_tasks(args.profile), rds, args.mgmt_gateway, args.aws_gateway)
    if args.write:
        POLICY.write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
    elif json.loads(POLICY.read_text(encoding="utf-8")) != expected:
        raise SystemExit("On-prem ACL addresses changed. Rerun with --write and review the diff.")
    print("On-prem ACL matches the selected ECS tasks, RDS and router addresses.")
