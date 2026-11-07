"""Bounded checks of the deployed app, separate from Terraform completion.

Probe the registered target's private VM address or dedicated managed-service endpoint.
Only the configured readiness endpoint's HTTP 200 is evidence; redirects never count.
"""

import time
from datetime import UTC, datetime
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


def now():
    return datetime.now(UTC).isoformat(timespec="seconds")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class AppChecker:
    def __init__(self, config=None, node_checker=None):
        self.node_checker = node_checker
        config = config or {}
        self.deadline = max(0, min(float(config.get("deadlineSeconds", 180)), 900))
        self.interval = max(0.01, min(float(config.get("intervalSeconds", 5)), 30))
        self.timeout = max(0.01, min(float(config.get("requestTimeoutSeconds", 3)), 10))
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def check(self, handoff, app):
        if not app:
            return {
                "status": "NOT_CONFIGURED",
                "observedAt": now(),
                "reason": "No app image configured",
            }
        if (handoff.get("compute") or {}).get("runtime") == "ON_PREM" and self.node_checker:
            return self.node_checker(handoff)
        try:
            port = app.get("port", 80)
            path = app.get("health_path", "/readyz")
            if (
                not isinstance(port, int)
                or not 1 <= port <= 65535
                or not path.startswith("/")
                or path.startswith("//")
            ):
                raise ValueError("invalid readiness endpoint")
            compute = handoff["compute"]
            runtime = compute.get("runtime")
            if runtime in {"ECS_FARGATE", "CLOUD_RUN"}:
                url = compute["readiness_url"]
                parsed = urlsplit(url)
                if (
                    parsed.username
                    or parsed.password
                    or parsed.query
                    or parsed.fragment
                    or parsed.path != path
                ):
                    raise ValueError("invalid managed readiness URL")
                host = parsed.hostname or ""
                if runtime == "CLOUD_RUN":
                    if (
                        parsed.scheme != "https"
                        or not host.endswith(".run.app")
                        or parsed.port not in (None, 443)
                    ):
                        raise ValueError("expected Cloud Run HTTPS endpoint")
                elif (
                    parsed.scheme != "http"
                    or not host.endswith(".elb.amazonaws.com")
                    or (parsed.port or 80) != port
                ):
                    raise ValueError("expected app-specific load balancer")
            else:
                raise ValueError("unsupported runtime")
        except (KeyError, ValueError, TypeError):
            return {
                "status": "UNKNOWN",
                "observedAt": now(),
                "reason": "Missing or invalid target readiness endpoint",
            }
        started, end, attempts = now(), time.monotonic() + self.deadline, 0
        last_success = None
        while True:
            attempts += 1
            result = {
                "status": "NOT_READY",
                "startedAt": started,
                "observedAt": now(),
                "url": url,
                "attempts": attempts,
                "httpStatus": None,
                "lastSuccessAt": last_success,
                "scope": "APP_HTTP_READINESS",
            }
            try:
                req = Request(url, headers={"Cache-Control": "no-cache, no-store"})
                with self.opener.open(
                    req, timeout=min(self.timeout, max(0.01, end - time.monotonic()))
                ) as response:
                    result["httpStatus"] = response.status
                    result["status"] = "READY" if response.status == 200 else "NOT_READY"
                    result["reason"] = None if response.status == 200 else "Unexpected HTTP status"
            except HTTPError as exc:
                result.update(httpStatus=exc.code, reason="Unexpected HTTP status")
                exc.close()
            except (URLError, OSError, TimeoutError, HTTPException):
                result["reason"] = "App endpoint unreachable or timed out"
            result["observedAt"] = now()
            if result["status"] == "READY":
                result["lastSuccessAt"] = result["observedAt"]
                return result
            if time.monotonic() >= end:
                return result
            time.sleep(min(self.interval, max(0, end - time.monotonic())))
