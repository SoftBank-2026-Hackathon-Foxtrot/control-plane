resource "google_service_account" "app" {
  account_id   = "${var.name}-app"
  display_name = "${var.name} application"
  description  = "Managed by Terraform; deployment=${var.name}; owner=${var.labels.owner}"
}
resource "google_storage_bucket_iam_member" "objects" {
  bucket = var.bucket_name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.app.email}"
}
resource "google_project_iam_member" "runtime" {
  for_each = toset(["roles/logging.logWriter", "roles/monitoring.metricWriter", "roles/cloudsql.client", "roles/cloudsql.instanceUser", "roles/artifactregistry.reader"])
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.app.email}"
}

# S3-compatible access for the app (it talks to object storage through boto3).
resource "google_storage_hmac_key" "app" {
  service_account_email = google_service_account.app.email
}
