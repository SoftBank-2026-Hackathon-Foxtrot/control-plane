terraform {
  required_version = ">= 1.10.0, < 2.0.0"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 7.0" }
  }
  # One state per routed network. bucket and key come from -backend-config:
  #   terraform init -backend-config="bucket=foxtrot-provisioner-state-<account>" \
  #     -backend-config="key=tailscale/<cloud>/<router name>.tfstate"
  backend "s3" {
    region       = "ap-northeast-2"
    encrypt      = true
    use_lockfile = true
  }
}
provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

resource "google_project_service" "required" {
  for_each           = toset(["compute.googleapis.com", "iam.googleapis.com", "secretmanager.googleapis.com"])
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}
data "google_compute_network" "network" { name = var.network_name }
data "google_compute_subnetwork" "router" {
  name   = var.subnetwork_name
  region = var.region
}
locals {
  advertised_cidrs = setunion(toset([data.google_compute_subnetwork.router.ip_cidr_range]), var.service_cidrs)
  network_tag      = var.name
  forwarding_ports = sort([for port in setunion(toset([5432]), var.redis_ports, var.app_ports, var.service_ports) : tostring(port)])
}
module "bootstrap" {
  source            = "../../../modules/tailscale_bootstrap"
  name              = var.name
  cloud             = "gcp"
  region            = var.region
  secret_ref        = "projects/${var.project_id}/secrets/${var.auth_key_secret_id}"
  advertised_cidrs  = local.advertised_cidrs
  remote_cidrs      = var.remote_cidrs
  tailscale_version = var.tailscale_version
}

resource "google_service_account" "router" {
  account_id   = var.name
  display_name = "Foxtrot shared Tailscale router"
  depends_on   = [google_project_service.required]
}
resource "google_secret_manager_secret_iam_member" "auth_key" {
  project   = var.project_id
  secret_id = var.auth_key_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.router.email}"
}

resource "google_compute_instance" "router" {
  name           = var.name
  machine_type   = var.machine_type
  zone           = var.zone
  can_ip_forward = true
  tags           = [local.network_tag]
  labels         = merge(var.labels, { component = "tailscale-router", managed_by = "terraform" })
  boot_disk {
    initialize_params {
      image = var.image
      size  = 10
      type  = "pd-balanced"
    }
  }
  network_interface {
    subnetwork = data.google_compute_subnetwork.router.self_link
    access_config {}
  }
  service_account {
    email  = google_service_account.router.email
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
  }
  metadata                = { enable-oslogin = "TRUE", block-project-ssh-keys = "TRUE" }
  metadata_startup_script = module.bootstrap.startup_script
  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }
  depends_on = [google_secret_manager_secret_iam_member.auth_key, google_compute_firewall.clients]
  lifecycle {
    precondition {
      condition     = data.google_compute_subnetwork.router.network == data.google_compute_network.network.self_link
      error_message = "The existing router subnet must belong to the selected VPC network."
    }
  }
}

resource "google_compute_firewall" "clients" {
  name          = "${var.name}-db-forwarding"
  network       = data.google_compute_network.network.self_link
  target_tags   = [local.network_tag]
  source_ranges = local.advertised_cidrs
  allow {
    protocol = "tcp"
    ports    = local.forwarding_ports
  }
}
resource "google_compute_firewall" "wireguard" {
  name          = "${var.name}-wireguard"
  network       = data.google_compute_network.network.self_link
  target_tags   = [local.network_tag]
  source_ranges = ["0.0.0.0/0"]
  allow {
    protocol = "udp"
    ports    = ["41641"]
  }
}
resource "google_compute_firewall" "database_clients" {
  for_each      = var.database_network_tags
  name          = "${var.name}-db-${substr(sha1(each.value), 0, 8)}"
  network       = data.google_compute_network.network.self_link
  target_tags   = [each.value]
  source_ranges = var.remote_cidrs
  allow {
    protocol = "tcp"
    ports    = ["5432"]
  }
}
resource "google_compute_firewall" "remote_clients" {
  count                   = length(var.client_service_account_emails) == 0 ? 0 : 1
  name                    = "${var.name}-client-db-egress"
  network                 = data.google_compute_network.network.self_link
  direction               = "EGRESS"
  priority                = 900
  target_service_accounts = var.client_service_account_emails
  destination_ranges      = var.remote_cidrs
  allow {
    protocol = "tcp"
    ports    = local.forwarding_ports
  }
}
resource "google_compute_firewall" "remote_tagged_clients" {
  count              = length(var.client_network_tags) == 0 ? 0 : 1
  name               = "${var.name}-tagged-client-egress"
  network            = data.google_compute_network.network.self_link
  direction          = "EGRESS"
  priority           = 900
  target_tags        = var.client_network_tags
  destination_ranges = var.remote_cidrs
  allow {
    protocol = "tcp"
    ports    = local.forwarding_ports
  }
}
resource "google_compute_firewall" "cache_clients" {
  for_each      = var.cache_network_tags
  name          = "${var.name}-cache-${substr(sha1(each.value), 0, 8)}"
  network       = data.google_compute_network.network.self_link
  target_tags   = [each.value]
  source_ranges = var.remote_cidrs
  allow {
    protocol = "tcp"
    ports    = [for port in var.redis_ports : tostring(port)]
  }
}
resource "google_compute_route" "remote" {
  for_each          = var.remote_cidrs
  name              = "${var.name}-${replace(replace(each.value, ".", "-"), "/", "-")}"
  network           = data.google_compute_network.network.self_link
  dest_range        = each.value
  next_hop_instance = google_compute_instance.router.self_link
  priority          = 900
}
resource "google_compute_network_peering_routes_config" "private_services" {
  count                = var.private_services_peering_name == null ? 0 : 1
  project              = var.project_id
  network              = data.google_compute_network.network.name
  peering              = var.private_services_peering_name
  export_custom_routes = true
  import_custom_routes = coalesce(var.private_services_import_custom_routes, false)
  depends_on           = [google_compute_route.remote]
  lifecycle {
    precondition {
      condition     = var.private_services_import_custom_routes != null
      error_message = "Explicitly preserve the existing PSA peering import_custom_routes setting."
    }
  }
}

output "instance_name" { value = google_compute_instance.router.name }
output "private_ip" { value = google_compute_instance.router.network_interface[0].network_ip }
output "advertised_cidrs" { value = local.advertised_cidrs }
output "routing_scope" {
  value = {
    network                       = var.network_name, remote_cidrs = var.remote_cidrs,
    private_services_peering_name = var.private_services_peering_name, db_port = 5432, redis_ports = var.redis_ports
  }
}

resource "google_compute_firewall" "remote_app" {
  count                   = length(var.app_ports) > 0 && length(var.client_service_account_emails) > 0 ? 1 : 0
  name                    = "${var.name}-remote-app"
  network                 = data.google_compute_network.network.self_link
  direction               = "INGRESS"
  priority                = 900
  source_ranges           = var.remote_cidrs
  target_service_accounts = var.client_service_account_emails
  allow {
    protocol = "tcp"
    ports    = [for p in var.app_ports : tostring(p)]
  }
}
