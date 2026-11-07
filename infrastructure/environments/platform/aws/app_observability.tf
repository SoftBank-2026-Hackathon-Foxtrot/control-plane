# Apps in either cloud use the same collector credential, fetched at runtime.
data "aws_secretsmanager_secret_version" "app_otel_headers" {
  count     = var.observability == null ? 0 : 1
  secret_id = var.observability == null ? "" : var.observability.headers_secret_arn
}
resource "google_secret_manager_secret" "app_otel_headers" {
  count     = var.observability == null ? 0 : 1
  project   = var.gcp_project_id
  secret_id = "foxtrot-app-otel-headers"
  replication {
    auto {}
  }
}
resource "google_secret_manager_secret_version" "app_otel_headers" {
  count       = var.observability == null ? 0 : 1
  secret      = google_secret_manager_secret.app_otel_headers[0].id
  secret_data = data.aws_secretsmanager_secret_version.app_otel_headers[0].secret_string
}
