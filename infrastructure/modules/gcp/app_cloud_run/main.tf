terraform {
  required_providers { google = { source = "hashicorp/google" } }
}
variable "name" { type = string }
variable "region" { type = string }
variable "network_id" { type = string }
variable "subnetwork_id" { type = string }
variable "service_account" { type = string }
variable "labels" { type = map(string) }
variable "app" { type = any }
variable "environment" { type = map(string) }
variable "bootstrap_environment" { type = map(string) }
variable "entrypoint" { type = list(string) }
variable "runtime_secrets" { type = map(string) }
variable "proxy_cidrs" { type = set(string) }
variable "service_cidr" { type = string }

locals {
  network_tag = "${var.name}-run"
  containers = concat([
    { name = "api", args = ["api"], environment = merge(var.environment, var.bootstrap_environment, var.app.api_env) }
    ], [for index in range(var.app.worker_count) : {
      name = "worker-${index + 1}", args = ["w${index + 1}"], environment = merge(var.environment, var.bootstrap_environment, var.app.worker_env)
  }])
  secrets = merge(var.runtime_secrets, var.app.secret_env)
}
resource "google_cloud_run_v2_service" "this" {
  name                = var.name
  location            = var.region
  deletion_protection = false
  # The service-specific HTTPS URL replaces the former public VM endpoint.
  ingress = "INGRESS_TRAFFIC_ALL"
  labels  = var.labels
  template {
    service_account = var.service_account
    timeout         = "3600s"
    scaling {
      min_instance_count = 1
      max_instance_count = 1
    }
    vpc_access {
      egress = "PRIVATE_RANGES_ONLY"
      network_interfaces {
        network    = var.network_id
        subnetwork = var.subnetwork_id
        tags       = [local.network_tag]
      }
    }
    dynamic "containers" {
      for_each = { for c in local.containers : c.name => c }
      content {
        name       = containers.value.name
        depends_on = containers.key == "api" ? [] : ["api"]
        image      = var.app.image
        command    = var.entrypoint
        args       = containers.value.args
        # Queue workers remain runnable when there is no incoming HTTP request.
        resources {
          limits   = { cpu = tostring(var.app.cpu), memory = "${var.app.memory_mib}Mi" }
          cpu_idle = false
        }
        dynamic "ports" {
          for_each = containers.key == "api" ? [var.app.port] : []
          content { container_port = ports.value }
        }
        dynamic "startup_probe" {
          for_each = containers.key == "api" ? [1] : []
          content {
            initial_delay_seconds = 10
            period_seconds        = 10
            failure_threshold     = 24
            http_get {
              path = var.app.health_path
              port = var.app.port
            }
          }
        }
        dynamic "env" {
          for_each = { for k, v in containers.value.environment : k => v if !contains(keys(local.secrets), k) }
          content {
            name  = env.key
            value = env.value
          }
        }
        dynamic "env" {
          for_each = local.secrets
          content {
            name = env.key
            value_source {
              secret_key_ref {
                secret  = env.value
                version = "latest"
              }
            }
          }
        }
      }
    }
    dynamic "containers" {
      for_each = var.app.tunnel_secret == null ? [] : [1]
      content {
        name  = "cloudflared"
        image = var.app.cloudflared_image
        args  = ["tunnel", "--no-autoupdate", "run"]
        resources {
          limits   = { cpu = "1", memory = "256Mi" }
          cpu_idle = false
        }
        env {
          name = "TUNNEL_TOKEN"
          value_source {
            secret_key_ref {
              secret  = var.app.tunnel_secret
              version = "latest"
            }
          }
        }
      }
    }
  }
}
resource "google_cloud_run_v2_service_iam_member" "public" {
  name     = google_cloud_run_v2_service.this.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}
# Cloud Run Direct VPC firewall matching uses network tags, not VM service-account selectors.
resource "google_compute_firewall" "proxy" {
  count              = length(var.proxy_cidrs) == 0 ? 0 : 1
  name               = "${var.name}-run-proxy"
  network            = var.network_id
  direction          = "EGRESS"
  priority           = 900
  target_tags        = [local.network_tag]
  destination_ranges = var.proxy_cidrs
  allow {
    protocol = "tcp"
    ports    = ["6379", "6432", "7000", "8000", "8001", "8080", "8090"]
  }
}
resource "google_compute_firewall" "data" {
  name               = "${var.name}-run-data"
  network            = var.network_id
  direction          = "EGRESS"
  priority           = 1000
  target_tags        = [local.network_tag]
  destination_ranges = [var.service_cidr]
  allow {
    protocol = "tcp"
    ports    = ["5432", "3307", "6378", "6379"]
  }
}
resource "google_compute_firewall" "private_deny" {
  name               = "${var.name}-run-private-deny"
  network            = var.network_id
  direction          = "EGRESS"
  priority           = 2000
  target_tags        = [local.network_tag]
  destination_ranges = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"]
  deny { protocol = "all" }
}
output "service_id" { value = google_cloud_run_v2_service.this.id }
output "url" { value = google_cloud_run_v2_service.this.uri }
output "network_tag" { value = local.network_tag }
