# Pre-resources for user apps on GCP, made once per project: the image registry and the secret
# slots app VMs read at boot. Everything else is created per environment by environments/gcp.
terraform {
  required_version = ">= 1.10.0, < 2.0.0"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 7.0" }
  }
}

variable "project_id" { type = string }
variable "region" {
  type    = string
  default = "asia-northeast3"
}
variable "repository" {
  type    = string
  default = "apps"
}
variable "secret_slots" {
  type        = map(string)
  default     = { "foxtrot-tailscale-authkey-gcp" = "Tailscale auth key (tag:gcp) for app VMs" }
  description = "값 없이 만들어 두는 비밀 자리. 값은 소유 파트가 넣습니다."
}

provider "google" {
  project = var.project_id
  region  = var.region
}

resource "google_project_service" "required" {
  for_each           = toset(["artifactregistry.googleapis.com", "secretmanager.googleapis.com", "cloudresourcemanager.googleapis.com", "servicenetworking.googleapis.com"])
  service            = each.value
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "apps" {
  location      = var.region
  repository_id = var.repository
  format        = "DOCKER"
  description   = "User app images pulled by app VMs at boot"
  labels        = { project = "foxtrot", managed_by = "terraform", component = "registry" }
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions { keep_count = 10 }
  }
  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret" "slot" {
  for_each    = var.secret_slots
  secret_id   = each.key
  labels      = { project = "foxtrot", managed_by = "terraform", component = "secret-slot" }
  annotations = { purpose = each.value }
  replication {
    auto {}
  }
  depends_on = [google_project_service.required]
}

output "image_prefix" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.apps.repository_id}/"
}
output "secret_slots" {
  value = { for k, s in google_secret_manager_secret.slot : k => s.id }
}
