"""Run one COMPUTE adapter on its on-prem node: python -m compute.server --help"""

import argparse
import hmac
import json
import os
import re
import threading
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from control_plane.deployment.tools.telemetry import get_telemetry

from .adapter import Adapter, Problem


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def dispatch(self):
        try:
            authorization = self.headers.get("Authorization", "").encode()
            if not hmac.compare_digest(authorization, self.server.authorization):
                raise Problem(401, "UNAUTHENTICATED", "Service bearer token required")
            code, location, payload = self.route()
        except Problem as error:
            code, location = error.status, None
            payload = {
                "code": error.code,
                "message": str(error),
                "operationId": None,
                "blockers": [],
            }
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if location:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(data)

    def route(self):
        adapter = self.server.adapter
        if self.command == "GET":
            if self.path == "/v1/capabilities":
                return 200, None, adapter.capabilities()
            match = re.fullmatch(r"/v1/(migrations|operations)/([\w-]+)", self.path)
            if match:
                return 200, None, adapter.get(*match.groups())
        else:
            match = re.fullmatch(r"/v1/migrations/([\w-]+)/actions", self.path)
            if match or self.path == "/v1/migrations":
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    if not 0 < length <= 65536:
                        raise ValueError()
                    body = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeError):
                    raise Problem(
                        400, "INVALID_REQUEST", ("Expected a JSON body up to 64 KiB")
                    ) from None
                return adapter.post(
                    match[1] if match else None, self.headers.get("Idempotency-Key"), body
                )
        raise Problem(404, "NOT_FOUND", "Unknown endpoint")


def work(adapter, stopping):
    while not stopping.is_set():
        if not adapter.run_next():
            stopping.wait(0.25)


def observe(adapter, stopping):
    while not stopping.is_set():
        with suppress(Exception):
            adapter.publish_observations()
        stopping.wait(5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--handoff", type=Path, required=True)
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    token = os.environ.get("COMPUTE_TOKEN")
    if not token:
        parser.error("Set COMPUTE_TOKEN for the control-plane client")
    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as service:
        service.authorization = ("Bearer " + token).encode()
        service.adapter = Adapter(
            args.db, args.binding, json.loads(args.handoff.read_text(encoding="utf-8-sig"))
        )
        stopping = threading.Event()
        worker = threading.Thread(target=work, args=(service.adapter, stopping))
        worker.start()
        observer = threading.Thread(target=observe, args=(service.adapter, stopping), daemon=True)
        observer.start()
        try:
            service.serve_forever()
        finally:
            stopping.set()
            worker.join()
            observer.join(timeout=3)
            get_telemetry("iac").close()


if __name__ == "__main__":
    main()
