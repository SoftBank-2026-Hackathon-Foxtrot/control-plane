"""Turn an environment request (org, cloud, app) into reviewed Terraform inputs.

The request names who the environment is for and which cloud it goes to. Everything else
(network plan, sizing baseline, registry image, account/project) comes from the server's
config, so a caller cannot pick an unreviewed account, image or size. The result is checked by
the same validator the CLI uses (tools/render_inputs.py)."""

from __future__ import annotations

import copy
import datetime as dt
import re

from control_plane.deployment.tools import render_inputs as inputs


class RequestError(ValueError):
    """The request cannot be turned into an environment (400)."""


ORG_ID = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")
SUFFIX = re.compile(r"^[a-f0-9]{4}$")
APP_FIELDS = {  # request (camelCase) -> Terraform app object
    "cpu": "cpu",
    "memoryMiB": "memory_mib",
    "image": "image",
    "apiWorkers": "api_workers",
    "workerCount": "worker_count",
    "seedDemoData": "seed_demo_data",
    "env": "env",
    "apiEnv": "api_env",
    "workerEnv": "worker_env",
    "secretEnv": "secret_env",
    "tunnelSecret": "tunnel_secret",
    "port": "port",
    "healthPath": "health_path",
}
ACCESS_FIELDS = {
    "onprem": {},
    "aws": {
        "replicationCidrs": "replication_cidrs",
        "proxyCidrs": "proxy_cidrs",
        "migrationSourceCidrs": "migration_source_cidrs",
        "cloudflareTunnel": "enable_cloudflare_tunnel",
        "webIngressCidrs": "web_ingress_cidrs",
    },
    "gcp": {
        "dbAuthorizedNetworks": "db_authorized_networks",
        "proxyCidrs": "proxy_cidrs",
        "cloudflareTunnel": "enable_cloudflare_tunnel",
        "webIngressCidrs": "web_ingress_cidrs",
    },
}


def require(condition, message):
    if not condition:
        raise RequestError(message)


def deployment_id(org_id: str, cloud: str, suffix: str) -> str:
    """Short, label-safe and unique per environment: <org up to 10>-<cloud>-<4 hex>."""
    require(SUFFIX.match(suffix) is not None, "suffix must be 4 hex characters")
    slug = org_id[:10].rstrip("-")
    return f"{slug}-{cloud}-{suffix}"


def parse(request: dict, catalog: dict, config: dict) -> dict:
    """Check the request's shape and return its normalised form (no cloud calls)."""
    require(type(request) is dict, "body must be a JSON object")
    require(
        not (
            request.keys()
            - {"org", "cloud", "app", "target", "sizing", "access", "mode", "workflowId"}
        ),
        "unknown top-level field",
    )
    workflow = request.get("workflowId")
    require(
        workflow is None
        or (type(workflow) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", workflow)),
        "invalid workflowId",
    )
    org = request.get("org")
    require(
        type(org) is dict and type(org.get("id")) is str and ORG_ID.match(org["id"]) is not None,
        "org.id: 3-32 lowercase letters, digits or hyphens, starting with a letter",
    )
    require(not (org.keys() - {"id", "name"}), "org: only id and name")
    cloud = request.get("cloud")
    require(type(cloud) is str and cloud in config["clouds"], f"cloud: one of {sorted(config['clouds'])}")
    app = request.get("app") or {}
    require(type(app) is dict, "app must be an object")
    template = app.get("template", "chiikawa-groupbuy")
    require(
        type(template) is str and template in catalog["templates"], f"app.template: one of {sorted(catalog['templates'])}"
    )
    require(not (app.keys() - set(APP_FIELDS) - {"template"}), "app: unknown field")
    mode = request.get("mode", "apply")
    require(mode in ("apply", "plan"), "mode: apply or plan")
    for field in ("target", "sizing", "access"):
        require(type(request.get(field, {})) is dict, f"{field} must be an object")
    require(
        not (request.get("access", {}).keys() - ACCESS_FIELDS[cloud].keys()),
        f"access: allowed fields for {cloud} are {sorted(ACCESS_FIELDS[cloud])}",
    )
    return {
        "workflowId": workflow,
        "org": {"id": org["id"], "name": org.get("name", org["id"])},
        "cloud": cloud,
        "template": template,
        "app": {k: v for k, v in app.items() if k != "template"},
        "target": request.get("target", {}),
        "sizing": request.get("sizing", {}),
        "access": request.get("access", {}),
        "mode": mode,
    }


def render(req: dict, catalog: dict, config: dict, suffix: str, today: dt.date) -> dict:
    """Terraform inputs for this request. Raises RequestError on anything not allowed."""
    if req["cloud"] == "onprem":
        node_id = req["target"].get("nodeId")
        require(
            set(req["target"]) == {"nodeId"}
            and type(node_id) is str
            and node_id in config["clouds"]["onprem"]["nodes"],
            "target.nodeId must name a registered on-prem node",
        )
        template = catalog["templates"][req["template"]]
        app = copy.deepcopy(template.get("app", {}))
        require("image" in req["app"], "on-prem deployment requires app.image")
        for key, value in req["app"].items():
            app[APP_FIELDS[key]] = value
        require(
            type(app.get("port", 80)) is int and 1 <= app.get("port", 80) <= 65535,
            "invalid app.port",
        )
        for field in ("worker_count", "api_workers"):
            count = app.get(field, 2 if field == "worker_count" else 4)
            require(type(count) is int and 1 <= count <= 8, f"invalid {field}")
        require(
            type(app.get("health_path", "/readyz")) is str
            and re.fullmatch(r"/[A-Za-z0-9/_-]*", app.get("health_path", "/readyz")),
            "healthPath must be a local HTTP path",
        )
        require(not app.get("secret_env"), "on-prem secrets belong to the registered node")
        require(
            not app.get("tunnel_secret"),
            "on-prem ingress belongs to the registered node",
        )
        for field in ("env", "api_env", "worker_env"):
            values = app.get(field, {})
            require(type(values) is dict, f"{field} must be an object")
            require(
                all(
                    type(key) is str
                    and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                    and type(value) is str
                    and "\n" not in value
                    and "\r" not in value
                    for key, value in values.items()
                ),
                f"{field} must contain valid single-line environment variables",
            )
        node = config["clouds"]["onprem"]["nodes"][node_id]
        require(
            type(app["image"]) is str
            and any(app["image"].startswith(prefix) for prefix in node["imagePrefixes"]),
            "image is not allowed on this node",
        )
        require(
            not req["sizing"] and not req["access"],
            "on-prem capacity and access are configured on the registered node",
        )
        from control_plane.deployment.runtime import entrypoint

        app["entrypoint"] = entrypoint(app.get("port", 80), app.get("api_workers", 4))
        return {
            "deployment_id": deployment_id(req["org"]["id"], "node", suffix),
            "node_id": node_id,
            "app": app,
            "sizing": {},
        }

    cloud, cloud_conf = req["cloud"], config["clouds"][req["cloud"]]
    template = catalog["templates"][req["template"]]
    dep = deployment_id(req["org"]["id"], cloud, suffix)

    app = copy.deepcopy(template.get("app", {}))
    app.pop("runtime", None)
    app.setdefault("cpu", 1)
    app.setdefault("memory_mib", 2048)
    image = req["app"].get("image") or cloud_conf.get("images", {}).get(req["template"])
    require(image is not None, f"no {cloud} image configured for {req['template']}; pass app.image")
    allowed_prefixes = cloud_conf.get("imagePrefixes")
    if req["app"].get("image") and allowed_prefixes:
        require(
            any(image.startswith(p) for p in allowed_prefixes),
            "app.image: registry repository is not allowed",
        )
    port = req["app"].get("port", app.get("port", 80))
    require(type(port) is int and 1 <= port <= 65535, "app.port: an integer port")
    for key, value in req["app"].items():
        app[APP_FIELDS[key]] = value
    app["image"] = image
    from control_plane.deployment.runtime import entrypoint

    app["entrypoint"] = entrypoint(port, app.get("api_workers", 4))
    # Platform defaults (server config "appEnv"): the app reaches its stores through the
    # platform's proxies so a migration can swap what is behind them. Request env wins.
    if config.get("appEnv"):
        app["env"] = {**config["appEnv"], **app.get("env", {})}

    tfvars = copy.deepcopy(cloud_conf["baseline"])
    target = req["target"]
    region = target.get("region", cloud_conf["region"])
    require(
        region in cloud_conf.get("regions", [cloud_conf["region"]]), "target.region is not allowed"
    )
    if cloud == "aws":
        account = target.get("accountId", cloud_conf["accounts"][0])
        require(account in cloud_conf["accounts"], "target.accountId is not allowed")
        require(not (target.keys() - {"accountId", "region"}), "target: accountId and region only")
        tfvars.update(account_id=account, final_snapshot_identifier=f"{dep}-final")
        if region != cloud_conf["region"]:
            tfvars["network"]["availability_zones"] = [f"{region}a", f"{region}c"]
    else:
        project = target.get("projectId", cloud_conf["projects"][0])
        require(project in cloud_conf["projects"], "target.projectId is not allowed")
        require(
            not (target.keys() - {"projectId", "region", "zone"}),
            "target: projectId, region and zone only",
        )
        zone = target.get(
            "zone", cloud_conf["zone"] if region == cloud_conf["region"] else f"{region}-a"
        )
        tfvars.update(project_id=project, zone=zone)
    tfvars.update(
        deployment_id=dep,
        region=region,
        bucket_name=f"foxtrot-{dep}",
        database_name="app",
        deletion_protection=config.get("deletionProtection", False),
        force_destroy_storage=config.get("forceDestroyStorage", True),
        metadata={
            "project": "foxtrot",
            "environment": "poc",
            "owner": req["org"]["id"][:63],
            "cost_center": req["org"]["id"][:63],
            "workload": req["template"][:63],
            "migration_id": "none",
            "data_class": "internal",
            "expires_on": (today + dt.timedelta(days=config.get("expiresInDays", 30))).isoformat(),
        },
        app=app,
    )
    for key, field in ACCESS_FIELDS[cloud].items():
        if key in req["access"]:
            tfvars[field] = req["access"][key]
    if cloud_conf.get("tailscale"):  # private network to the platform, from the server config
        tfvars["tailscale"] = {
            "auth_key_secret": cloud_conf["tailscale"]["authKeySecret"],
            "tag": cloud_conf["tailscale"].get("tag", f"tag:{cloud}"),
        }
    if app.get("tunnel_secret"):  # cloudflared needs outbound 7844 to the Cloudflare edge
        tfvars["enable_cloudflare_tunnel"] = True
    try:
        if req["sizing"]:
            return inputs.merge_overrides(cloud, tfvars, req["sizing"])
        inputs.validate_config(cloud, tfvars)
    except ValueError as exc:
        raise RequestError(str(exc)) from exc
    return tfvars
