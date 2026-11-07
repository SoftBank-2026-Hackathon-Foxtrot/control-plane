"""python -m control_plane.deployment.provisioner {serve|up|down|render} --help

serve   run the HTTP API (PROVISIONER_TOKEN required)
up      build one environment now and print its handoff, e.g. the chiikawa app on AWS:
        python -m control_plane.deployment.provisioner up --org chiikawa --cloud aws
down    destroy an environment made by `up` or the API
render  print the Terraform inputs a request would produce, without touching any cloud
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import threading
from pathlib import Path

from control_plane.deployment.paths import iac_root

from .server import ProvisionerServer
from .service import Conflict, Service
from .spec import RequestError, parse, render
from .store import Store
from .terraform import Terraform

HERE = iac_root() / "provisioner"


def load(path: str | None) -> tuple[dict, dict]:
    catalog = json.loads((HERE / "catalog.json").read_text())
    if not path and os.environ.get("PROVISIONER_CONFIG_JSON"):  # containers pass the config inline
        return catalog, json.loads(os.environ["PROVISIONER_CONFIG_JSON"])
    config_path = Path(path or os.environ.get("PROVISIONER_CONFIG", HERE / "config.json"))
    if not config_path.exists():
        sys.exit(f"config not found: {config_path} (copy provisioner/config.example.json)")
    return json.loads((HERE / "catalog.json").read_text()), json.loads(config_path.read_text())


def build(config: dict, catalog: dict) -> Service:
    work = Path(config["workRoot"])
    work.mkdir(parents=True, exist_ok=True)
    if cache := os.environ.get("TF_PLUGIN_CACHE_DIR"):
        Path(cache).mkdir(parents=True, exist_ok=True)
    terraform = Terraform(
        HERE.parent,
        work,
        binary=os.environ.get("TERRAFORM_BIN", "terraform"),
        state=config.get("stateBackend"),
    )
    from .node import DeploymentExecutor
    from .readiness import AppChecker

    executor = DeploymentExecutor(
        terraform, config.get("clouds", {}).get("onprem", {}).get("nodes", {})
    )
    service = Service(
        Store(work / "provisioner.db"),
        executor,
        catalog,
        config,
        checker=AppChecker(config.get("readiness"), node_checker=executor.check),
    )

    if config.get("onprem"):
        from .drivers.ansible import AnsibleDriver
        from .inventory import Inventory
        from .onprem import OnPremService
        from .onprem_store import OnPremStore

        settings = config["onprem"]
        inventory = Inventory(
            settings["catalogFile"],
            settings["secretRoot"],
            allow_local=settings.get("allowLocal", False),
        )
        service.onprem = OnPremService(
            OnPremStore(work / "provisioner.db"),
            inventory,
            AnsibleDriver(HERE.parent, work / "ansible", inventory),
            service.checker,
        )
    return service


def request_from(args) -> dict:
    body = {"org": {"id": args.org}, "cloud": args.cloud, "app": {"template": args.template}}
    if args.image:
        body["app"]["image"] = args.image
    if args.plan_only:
        body["mode"] = "plan"
    if args.workflow_id:
        body["workflowId"] = args.workflow_id
    return body


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m control_plane.deployment.provisioner",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        help="server config JSON (default: $PROVISIONER_CONFIG or provisioner/config.json)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8070)
    for name in ("up", "render"):
        p = sub.add_parser(name)
        p.add_argument("--org", required=True)
        p.add_argument("--cloud", required=True, choices=["aws", "gcp"])
        p.add_argument("--template", default="chiikawa-groupbuy")
        p.add_argument("--image", help="override the configured registry image")
        p.add_argument(
            "--workflow-id", help="control-plane workflow to receive preparation observations"
        )
        p.add_argument("--plan-only", action="store_true", help="stop after the plan (up only)")
    down = sub.add_parser("down")
    down.add_argument("environment_id")
    onprem = sub.add_parser("onprem-up", help="create a registered onprem plan or apply request")
    onprem.add_argument("--request", required=True, help="v2 request JSON file")
    onprem.add_argument("--idempotency-key", required=True)
    args = parser.parse_args()
    catalog, config = load(args.config)

    try:
        if args.command == "render":
            req = parse(request_from(args), catalog, config)
            print(
                json.dumps(
                    render(req, catalog, config, "0000", dt.date.today()),
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return
        service = build(config, catalog)
        service.recover()
        if args.command == "serve":
            token = os.environ.get("PROVISIONER_TOKEN")
            if not token or len(token) < 24:
                sys.exit("PROVISIONER_TOKEN (24+ characters) is required")
            stop = threading.Event()
            threading.Thread(target=service.run_forever, args=(stop,), daemon=True).start()
            print(f"provisioner listening on {args.host}:{args.port}", flush=True)
            ProvisionerServer((args.host, args.port), service, token).serve_forever()
            return
        if args.command == "onprem-up":
            if service.onprem is None:
                raise RequestError("onprem configuration required")
            _, created = service.onprem.create(
                json.loads(Path(args.request).read_text()), args.idempotency_key
            )
            while service.run_next():
                pass
            result = service.onprem.get(created["environmentId"])
            print(json.dumps(result, indent=2, ensure_ascii=False))
            if result["status"] not in ("PLANNED", "INFRA_READY", "READY"):
                sys.exit(1)
            return
        if args.command == "up":
            _, created = service.create(request_from(args))
            env_id = created["environmentId"]
        else:
            _, created = service.destroy(args.environment_id)
            env_id = args.environment_id
        print(f"{env_id}: {created['status']} (log: {service.tf.log_path(env_id)})", flush=True)
        while service.run_next():
            pass
        print(json.dumps(service.get(env_id), indent=2, ensure_ascii=False))
    except (RequestError, Conflict) as exc:
        sys.exit(f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
