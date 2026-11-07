output "network_id" {
  value = google_compute_network.this.id
}

output "subnetwork_id" {
  value = google_compute_subnetwork.app.id
}

output "service_range_name" {
  value = google_compute_global_address.services.name
}

output "network_name" { value = google_compute_network.this.name }
output "subnetwork_name" { value = google_compute_subnetwork.app.name }
output "private_services_peering_name" { value = google_service_networking_connection.this.peering }
