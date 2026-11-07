# App host metrics

The demo repository's `compose.observability.yml` runs a hostmetrics agent on each Linux application host. It exports to this stack's authenticated OTLP HTTP receiver; the existing metrics pipeline stores the data in Prometheus. Configure the agent's application ID, environment and stable host name to match the registry. The central Collector itself is not the measured application host.

Deploy the updated Prometheus configuration (resource labels `application.id`, `deployment.environment.name`, `host.name`) and this control-plane version together with the frontend's **Monitoring → System** screen. `CONTROL_PLANE_PROMETHEUS_URL` points to the private Prometheus endpoint. No exporter tokens reach the browser.

Authenticated API:

- `GET /v1/applications/{application_id}/system-metrics/hosts?environment=aws`
- `GET /v1/applications/{application_id}/system-metrics?environment=aws&host=goods-aws-01&category=cpu&minutes=15`

Categories: `cpu`, `memory`, `disk`, `network`. Windows: 15, 60, 360 minutes. Application/environment access follows the current release/2 shared seed registry. Unknown apps/environments return 404; arbitrary PromQL or backend URLs are never accepted. All queries additionally scope service and host. Responses are size-bounded, limited to at most 361 points, and return individual OK/NOT_CONFIGURED/UNAVAILABLE states. Missing/non-finite samples remain null. Gauges and rates stop producing current values after 30 seconds without host observations. Host discovery retains the normal Prometheus lookback window (about 5 minutes), with an explicit stale flag.

CPU uses average busy time across cores. Memory uses Linux MemAvailable and MemTotal. Disk capacity is for `/`; disk and network rates sum the agent's filtered devices/interfaces over 2 minutes. They describe the whole server, including other applications on a shared host. Duplicate agents for one application/environment/host are unsupported.

Verification: `pytest tests/test_system_metrics.py`; opt-in real agent/Prometheus check: `RUN_SYSTEM_METRICS_STACK=1 pytest tests/integration/test_system_metrics_stack.py` (see that file for local test ports). Application request, queue, chat and database instrumentation is outside this change.
