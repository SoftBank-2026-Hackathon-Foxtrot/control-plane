resource "google_storage_bucket" "this" {
  name                        = var.name
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = var.force_destroy
  versioning { enabled = true }
  labels = var.labels
}
