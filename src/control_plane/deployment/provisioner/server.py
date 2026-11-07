"""HTTP API. Every route except /health needs `Authorization: Bearer <PROVISIONER_TOKEN>`.

GET    /health
GET    /v1/templates
POST   /v1/environments              {org, cloud, app?, target?, sizing?, access?, mode?}  → 202
GET    /v1/environments
GET    /v1/environments/{id}
POST   /v1/environments/{id}/apply   apply a mode=plan environment's saved plan            → 202
POST   /v1/environments/{id}/update  {app?, sizing?, access?} merged into the environment  → 202
DELETE /v1/environments/{id}         destroy everything the environment created            → 202
GET    /v1/jobs/{id}                 status and the tail of the Terraform log
"""

from __future__ import annotations

import hmac
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .service import Conflict, NotFound, Service
from .spec import RequestError

ENV = re.compile(r"^/v1/environments/(env-[a-f0-9]{12})(/apply|/update|/check-app|/workflow)?$")
JOB = re.compile(r"^/v1/jobs/(job-[a-f0-9]{12})$")
ENV2 = re.compile(r"^/v2/environments/(env-[a-f0-9]{12})(?:/(plan|apply|check-app|workflow|reconcile))?$")
JOB2 = re.compile(r"^/v2/jobs/(job-[a-f0-9]{12})$")
MAX_BODY = 64 * 1024


class Handler(BaseHTTPRequestHandler):
    server: ProvisionerServer

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def do_DELETE(self):
        self.dispatch()

    def log_message(self, fmt, *args):  # no request bodies or headers in the log
        print(
            f"{self.address_string()} {self.command} {self.path.split('?')[0]} {args[1] if len(args) > 1 else ''}",
            flush=True,
        )

    def dispatch(self):
        try:
            code, payload = self.route()
        except RequestError as exc:
            code, payload = 400, {"code": "INVALID_REQUEST", "message": str(exc)}
        except Conflict as exc:
            code, payload = 409, {"code": exc.code, "message": str(exc), **exc.extra}
        except NotFound as exc:
            code, payload = 404, {"code": "NOT_FOUND", "message": f"{exc} not found"}
        except PermissionError:
            code, payload = 401, {"code": "UNAUTHENTICATED", "message": "Bearer token required"}
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise RequestError("invalid Content-Length") from None
        if not 0 <= length <= MAX_BODY:
            raise RequestError("body too large")
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise RequestError(f"invalid JSON: {exc.msg}") from exc

    def route(self) -> tuple[int, dict]:
        path = self.path.split("?")[0]
        if path == "/health" and self.command == "GET":
            return 200, {"status": "ok"}
        given = self.headers.get("Authorization", "").encode()
        if not hmac.compare_digest(given, self.server.authorization):
            raise PermissionError
        service: Service = self.server.service
        if path.startswith("/v2/"):
            if service.onprem is None:
                raise Conflict("ONPREM_NOT_CONFIGURED", "register the onprem catalog and credential directory first")
            if path == "/v2/environments":
                if self.command == "POST":
                    result = service.onprem.create(self.body(), self.headers.get("Idempotency-Key"))
                    service.wake.set()
                    return result
                if self.command == "GET":
                    return 200, {"environments": service.onprem.environments()}
            if m := ENV2.fullmatch(path):
                env_id, action = m.groups()
                if action and self.command == "POST":
                    result = service.onprem.command(env_id, action, self.body(), self.headers.get("Idempotency-Key"))
                    service.wake.set()
                    return result
                if action is None and self.command == "GET":
                    return 200, service.onprem.get(env_id)
            if (m := JOB2.fullmatch(path)) and self.command == "GET":
                return 200, service.onprem.job(m.group(1))
            raise NotFound(f"{self.command} {path}")
        if path == "/v1/templates" and self.command == "GET":
            return 200, {
                "templates": {k: v["description"] for k, v in service.catalog["templates"].items()},
                "clouds": sorted(service.config["clouds"]),
            }
        if path == "/v1/environments":
            if self.command == "POST":
                return service.create(self.body(), self.headers.get("Idempotency-Key"))
            if self.command == "GET":
                return 200, {
                    "environments": [service.view(e) for e in service.store.environments()]
                }
        if m := ENV.match(path):
            env_id, apply = m.group(1), m.group(2)
            if apply == "/apply" and self.command == "POST":
                return service.apply(env_id)
            if apply == "/update" and self.command == "POST":
                return service.update(env_id, self.body())
            if apply == "/check-app" and self.command == "POST":
                return service.check_app(env_id)
            if apply == "/workflow" and self.command == "POST":
                return service.bind_workflow(env_id, self.body())
            if not apply and self.command == "GET":
                return 200, service.get(env_id)
            if not apply and self.command == "DELETE":
                return service.destroy(env_id)
        if (m := JOB.match(path)) and self.command == "GET":
            return 200, service.job(m.group(1))
        raise NotFound(f"{self.command} {path}")


class ProvisionerServer(ThreadingHTTPServer):
    def __init__(self, address, service: Service, token: str):
        super().__init__(address, Handler)
        self.service, self.authorization = service, f"Bearer {token}".encode()
