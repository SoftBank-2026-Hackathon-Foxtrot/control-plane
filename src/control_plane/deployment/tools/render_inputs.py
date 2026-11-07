"""Validate a managed deployment, expose sizing options and merge selected values.

Only writes a reviewable tfvars JSON. Never calls Terraform, a cloud API or a shell.
Python standard library only; run from any working directory.
"""

import argparse
import copy
import ipaddress
import json
import math
import re
import sys
from pathlib import Path

from control_plane.deployment.paths import iac_root

ROOT = iac_root()
POLICY = json.loads((ROOT / "catalog/sizing-policy.json").read_text(encoding="utf-8"))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path):
    def reject_constant(value):
        raise ValueError(f"Non-finite JSON value: {value}")

    return json.loads(
        Path(path).read_text(encoding="utf-8-sig"),
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )


def validate_sizing(cloud, sizing, *, complete=True):
    require(cloud in POLICY["clouds"], "Unsupported cloud")
    require(type(sizing) is dict, "sizing must be an object")
    rules = POLICY["clouds"][cloud]
    require(not (sizing.keys() - rules.keys()), "Unknown or forbidden sizing parameter")
    if complete:
        require(
            sizing.keys() == rules.keys(), "Baseline must explicitly contain every sizing parameter"
        )
    for key, value in sizing.items():
        rule = rules[key]
        expected = {"string": str, "integer": int, "boolean": bool}[rule["type"]]
        require(type(value) is expected, f"{key}: expected {rule['type']}")
        if "enum" in rule:
            require(value in rule["enum"], f"{key}: value outside reviewed catalog")
        if "minimum" in rule:
            require(rule["minimum"] <= value <= rule["maximum"], f"{key}: outside allowed range")
    if complete:
        require(
            sizing["db_max_storage_gib"] >= math.ceil(sizing["db_storage_gib"] * 1.1),
            "DB autoscaling cap must be at least 110% of initial storage",
        )


def ipv4(cidr):
    result = ipaddress.ip_network(cidr, strict=True)
    require(result.version == 4, "Only IPv4 is supported by this PoC")
    return result


APP_KEYS = {
    "entrypoint",
    "cpu",
    "memory_mib",
    "image",
    "port",
    "api_workers",
    "worker_count",
    "served_by",
    "seed_demo_data",
    "seed_close_in_s",
    "env",
    "api_env",
    "worker_env",
    "secret_env",
    "tunnel_secret",
    "cloudflared_image",
    "health_path",
}
REGISTRY = {
    "aws": r"^[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/",
    "gcp": r"^[a-z0-9-]+-docker\.pkg\.dev/",
}


def validate_app(cloud, app):
    """The app image must already be in this cloud's registry; secrets only travel as references."""
    require(type(app) is dict and type(app.get("image")) is str, "app.image is required")
    require(not (app.keys() - APP_KEYS), "Unknown app key")
    require(
        re.match(REGISTRY[cloud], app["image"]) is not None,
        f"app.image must be in the {cloud} registry",
    )
    require("runtime" not in app, "VM and managed selectors are not supported")
    require(
        type(app.get("entrypoint")) is list
        and bool(app["entrypoint"])
        and all(type(v) is str for v in app["entrypoint"]),
        "app.entrypoint is supplied by the platform",
    )
    cpu, memory = app.get("cpu", 1), app.get("memory_mib", 2048)
    cpus = [0.25, 0.5, 1, 2, 4] if cloud == "aws" else [1, 2, 4]
    require(type(cpu) in (int, float) and cpu in cpus, "app.cpu: unsupported allocation")
    require(
        type(memory) is int and memory % 512 == 0 and memory >= 512,
        "app.memory_mib: use whole 512 MiB units",
    )
    if cloud == "aws":
        bounds = {
            0.25: (512, 2048),
            0.5: (1024, 4096),
            1: (2048, 8192),
            2: (4096, 16384),
            4: (8192, 30720),
        }
        low, high = bounds[cpu]
        require(
            low <= memory <= high and (cpu == 0.25 or memory % 1024 == 0),
            "app: invalid Fargate CPU/memory combination",
        )
    else:
        require(memory <= cpu * 4096, "app.memory_mib: exceeds supported Cloud Run allocation")
        require(
            cpu * (app.get("worker_count", 2) + 1) + bool(app.get("tunnel_secret")) <= 8,
            "app: combined Cloud Run containers exceed 8 vCPU",
        )
        require(
            memory * (app.get("worker_count", 2) + 1) + 256 * bool(app.get("tunnel_secret"))
            <= 32768,
            "app: combined Cloud Run containers exceed 32 GiB",
        )
    reserved = {
        "APP_DB_PASSWORD",
        "APP_STORAGE_SECRET",
        "APP_DB_USER",
        "DB_HOST",
        "DB_PORT",
        "DB_NAME",
        "DB_ADMIN_USER",
        "DB_ADMIN_PASSWORD",
    }
    if cloud == "gcp":
        reserved |= {"PORT", "K_SERVICE", "K_REVISION", "K_CONFIGURATION"}
    if True:
        require(
            not any(
                reserved & app.get(key, {}).keys()
                for key in ("env", "api_env", "worker_env", "secret_env")
            ),
            "app: reserved runtime environment variable",
        )
    path = app.get("health_path", "/readyz")
    require(
        type(path) is str
        and re.fullmatch(r"/[A-Za-z0-9_./-]*", path) is not None
        and not path.startswith("//"),
        "app.health_path must be a local HTTP path",
    )
    for field in ("env", "api_env", "worker_env", "secret_env"):
        values = app.get(field, {})
        require(
            type(values) is dict
            and all(type(v) is str and "\n" not in v and "'" not in v for v in values.values()),
            f"app.{field}: expected single-line strings",
        )
        require(
            all(re.match(r"^[A-Z_][A-Z0-9_]*$", k) for k in values),
            f"app.{field}: env names are UPPER_CASE",
        )


def validate_config(cloud, config):
    require(type(config) is dict, "Baseline must be a JSON object")
    common = {
        "deployment_id",
        "metadata",
        "region",
        "bucket_name",
        "database_name",
        "web_ingress_cidrs",
        "web_ports",
        "admin_cidrs",
        "deletion_protection",
        "network",
        "sizing",
    }
    extras = {
        "aws": {
            "account_id",
            "ami_id",
            "key_name",
            "postgres_version",
            "final_snapshot_identifier",
        },
        "gcp": {"project_id", "zone", "image"},
    }
    require(cloud in extras, "Unsupported cloud")
    optional = {
        "app",
        "postgres_version",
        "proxy_cidrs",
        "enable_cloudflare_tunnel",
        "force_destroy_storage",
        "tailscale",
    } | (
        {"migration_source_cidrs", "replication_cidrs", "redis_transit_encryption"}
        if cloud == "aws"
        else {"db_authorized_networks", "redis_auth", "redis_transit_encryption"}
    )
    require(
        not (config.keys() - common - extras[cloud] - optional),
        "Unknown baseline key; keep secrets outside JSON",
    )
    required = common | (
        {"account_id", "final_snapshot_identifier"} if cloud == "aws" else {"project_id", "zone"}
    )
    require(required <= config.keys(), "Missing required human-owned deployment settings")
    for field in ("proxy_cidrs", "migration_source_cidrs"):
        if field in config:
            require(type(config[field]) is list, f"{field}: expected a list")
            allowed = [
                ipaddress.ip_network(c)
                for c in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")
            ]
            for cidr in config[field]:
                n = ipv4(cidr)
                require(
                    n.prefixlen >= 24 and any(n.subnet_of(a) for a in allowed),
                    f"{field}: use private or Tailscale IPv4 /24 through /32",
                )
    require(
        type(config.get("enable_cloudflare_tunnel", False)) is bool,
        "enable_cloudflare_tunnel: expected boolean",
    )
    if config.get("app") is not None:
        validate_app(cloud, config["app"])
    validate_sizing(cloud, config["sizing"])
    require(
        config["web_ports"]
        and all(type(p) is int and p in (80, 443, 8080) for p in config["web_ports"]),
        "Only reviewed public application ports are allowed",
    )
    require(bool(config["web_ingress_cidrs"]), "At least one web ingress CIDR is required")
    for cidr in config["web_ingress_cidrs"]:
        ipv4(cidr)
    for cidr in config["admin_cidrs"]:
        require(ipv4(cidr).prefixlen >= 24, "SSH CIDRs must be /24 or narrower")
    n = config["network"]
    if cloud == "aws":
        require(
            set(n)
            == {"vpc_cidr", "public_subnet_cidr", "private_subnet_cidrs", "availability_zones"},
            "Unexpected AWS network fields",
        )
        require(len(n["private_subnet_cidrs"]) == 2, "Two private subnets are required")
        require(
            len(n["availability_zones"]) == len(set(n["availability_zones"])) == 2,
            "Two distinct AZs are required",
        )
        require(
            all(a.startswith(config["region"]) for a in n["availability_zones"]),
            "AZ/region mismatch",
        )
        vpc = ipv4(n["vpc_cidr"])
        subnets = [ipv4(c) for c in [n["public_subnet_cidr"], *n["private_subnet_cidrs"]]]
        require(all(s.subnet_of(vpc) for s in subnets), "Subnet outside VPC")
        require(
            not config["admin_cidrs"] or bool(config.get("key_name")),
            "SSH requires an existing EC2 key_name",
        )
    else:
        require(
            set(n) == {"subnet_cidr", "service_range_address", "service_range_prefix"},
            "Unexpected GCP network fields",
        )
        require(
            type(n["service_range_prefix"]) is int and 16 <= n["service_range_prefix"] <= 20,
            "PSA range prefix must be /16 through /20",
        )
        subnets = [
            ipv4(n["subnet_cidr"]),
            ipv4(f"{n['service_range_address']}/{n['service_range_prefix']}"),
        ]
        require(config["zone"].startswith(config["region"] + "-"), "Zone/region mismatch")
    require(
        not any(a.overlaps(b) for i, a in enumerate(subnets) for b in subnets[i + 1 :]),
        "Overlapping network ranges",
    )


def merge_overrides(cloud, baseline, overrides):
    """Merge UI-selected sizing into a platform-managed target baseline."""
    validate_config(cloud, baseline)
    validate_sizing(cloud, overrides, complete=False)
    result = copy.deepcopy(baseline)
    result["sizing"].update(overrides)
    validate_config(cloud, result)
    return result


def merge_recommendation(cloud, baseline, proposal):
    require(
        type(proposal) is dict and set(proposal) == {"cloud", "parameters", "rationale"},
        "Proposal must contain only cloud, parameters and rationale",
    )
    require(proposal["cloud"] == cloud, "Proposal cloud differs from the selected target")
    params, rationale = proposal["parameters"], proposal["rationale"]
    validate_sizing(cloud, params, complete=False)
    require(bool(params), "Empty recommendation")
    require(
        type(rationale) is dict and rationale.keys() == params.keys(),
        "Each recommendation needs a rationale",
    )
    require(
        all(type(v) is str and 1 <= len(v.strip()) <= 2000 for v in rationale.values()),
        "Rationale must be bounded, nonempty text",
    )
    return merge_overrides(cloud, baseline, params)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "context", "render", "render-overrides"):
        p = sub.add_parser(command)
        p.add_argument("--cloud", required=True, choices=["aws", "gcp"])
        p.add_argument("--base", required=True, type=Path)
        if command in ("render", "render-overrides"):
            p.add_argument(
                "--proposal" if command == "render" else "--request", required=True, type=Path
            )
            p.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        base = load_json(args.base)
        if args.command == "validate":
            validate_config(args.cloud, base)
            print(
                "Input checks passed. Run terraform validate/plan and review "
                "live capacity separately."
            )
        elif args.command == "context":
            validate_config(args.cloud, base)
            print(
                json.dumps(
                    {
                        "target_sizing": base["sizing"],
                        "sizing_schema": {
                            "type": "object",
                            "properties": POLICY["clouds"][args.cloud],
                            "additionalProperties": False,
                        },
                    },
                    indent=2,
                )
            )
        else:
            if args.command == "render":
                input_path = args.proposal
                result = merge_recommendation(args.cloud, base, load_json(input_path))
            else:
                input_path = args.request
                request = load_json(input_path)
                require(
                    type(request) is dict and set(request) == {"overrides"},
                    "Expected only overrides",
                )
                result = merge_overrides(args.cloud, base, request["overrides"])
            require(
                args.output.resolve() not in (args.base.resolve(), input_path.resolve()),
                "Output must not overwrite source inputs",
            )
            # Exclusive creation protects a previously reviewed proposal from silent replacement.
            with args.output.open("x", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
                f.write("\n")
            print(
                f"Wrote {args.output}. Human review and a saved Terraform plan are still required."
            )
    except (ValueError, TypeError, KeyError, OSError) as exc:
        print(f"Rejected: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
