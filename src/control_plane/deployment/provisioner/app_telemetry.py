"""Platform-owned exporter settings; only secret references enter deployment plans."""

from urllib.parse import urlsplit


def configure_app(app, config, cloud, *, owner, deployment):
    telemetry = config.get("appTelemetry")
    if not telemetry:
        return
    application = config.get("applicationId") or telemetry.get("applicationId")
    if not isinstance(application, str) or not application:
        raise ValueError("appTelemetry requires the configured applicationId")
    endpoint = telemetry.get("endpoint", "")
    address = urlsplit(endpoint)
    if (
        address.scheme not in {"http", "https"}
        or not address.hostname
        or address.username
        or address.password
        or address.query
        or address.fragment
    ):
        raise ValueError("appTelemetry requires an OTLP HTTP endpoint")
    secret = telemetry.get("headersSecrets", {}).get(cloud)
    if not secret:
        raise ValueError("appTelemetry requires an exporter headers secret for " + cloud)
    settings = {
        "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_APPLICATION_ID": application,
        "OTEL_SYSTEM_METRICS": "container",
        "OTEL_NETWORK_SCOPE": "app-instance",
        "OTEL_OWNER_ID": owner,
        "OTEL_DEPLOYMENT_ID": deployment,
        "OTEL_DEPLOYMENT_ENVIRONMENT": cloud,
        "OTEL_RESOURCE_ATTRIBUTES": "application.id="
        + application
        + ",deployment.environment.name="
        + cloud
        + ",foxtrot.owner.id="
        + owner
        + ",deployment.id="
        + deployment
        + ",foxtrot.deployment.id="
        + deployment
        + ",foxtrot.workload.kind=USER_APPLICATION",
    }
    for field in ("env", "api_env", "worker_env", "secret_env"):
        if any(key.startswith("OTEL_") for key in app.get(field, {})):
            raise ValueError("OTEL settings belong to appTelemetry")
    app["env"] = {**app.get("env", {}), **settings}
    app["secret_env"] = {**app.get("secret_env", {}), "OTEL_EXPORTER_OTLP_HEADERS": secret}

    app["api_env"] = {**app.get("api_env", {}), "OTEL_APP_ROLE": "api"}
    app["worker_env"] = {**app.get("worker_env", {}), "OTEL_APP_ROLE": "worker"}
