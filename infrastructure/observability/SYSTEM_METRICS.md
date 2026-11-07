# Application system metrics

The demo application exports container cgroup CPU, memory and disk I/O, plus network namespace counters. It never exports host totals or shared filesystem capacity. One exporter runs per container. Docker VM/on-prem app containers use their own network namespace. Managed Fargate/Cloud Run API containers export the shared app-instance network, including workers and the app tunnel; workers omit duplicate network counters. Unsupported counters remain absent.

Deploy the updated demo image, provisioner, Prometheus resource promotion config, control-plane and frontend together. `CONTROL_PLANE_PROMETHEUS_URL` is the private query endpoint. Exporter headers stay in secret stores and never reach the browser. Old host-agent series are excluded; stop any old demo `otel-system` agent.

- `GET /v1/applications/{application_id}/system-metrics/instances?deployment={environmentId}`
- `GET /v1/applications/{application_id}/system-metrics?deployment={environmentId}&instance={serviceInstanceId}&category=cpu&minutes=15`

The authenticated user must own the deployment record. `deployment` is the platform environment ID, resolved server-side to its cloud and persisted deployment ID. Missing/foreign deployments return 404. Queries require exact application, owner, deployment, environment, runtime service and instance labels. There is no application-name, host or environment-only fallback. Values cannot be combined across deployments. Arbitrary PromQL/backend URLs are not accepted.

Categories are CPU, memory, disk I/O and network. Windows are 15/60/360 minutes, at most 361 points. Each series returns OK/NOT_CONFIGURED/UNAVAILABLE. CPU cores and memory bytes are independent of optional quota percentages. Rates use a two-minute window. No samples for 30 seconds means null, not zero; instance discovery has a stale flag and the normal Prometheus lookback. A new exporter instance ID is generated after an exporter restart.

This is scoped monitoring within the shared platform, not cryptographic tenant isolation: the collector currently uses a shared trusted emitter token. Do not give that token to arbitrary untrusted applications. General multi-tenant support requires per-deployment credentials and collector-side resource-label enforcement, as well as application-specific migration configuration.
