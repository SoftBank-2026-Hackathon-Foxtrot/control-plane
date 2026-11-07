resource "google_compute_network" "this" {
  name                    = var.name
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
  description             = jsonencode(var.labels)
}
resource "google_compute_subnetwork" "app" {
  name                     = "${var.name}-app"
  region                   = var.region
  network                  = google_compute_network.this.id
  ip_cidr_range            = var.subnet_cidr
  private_ip_google_access = true
  description              = jsonencode(var.labels)
}
resource "google_compute_global_address" "services" {
  name          = "${var.name}-services"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  address       = var.service_range_address
  prefix_length = var.service_range_prefix
  network       = google_compute_network.this.id
  labels        = var.labels
}
resource "google_service_networking_connection" "this" {
  network                 = google_compute_network.this.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.services.name]
}
