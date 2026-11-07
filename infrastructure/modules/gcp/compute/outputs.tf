output "instance_id" {
  value = google_compute_instance.this.id
}

output "public_ip" {
  value = google_compute_address.this.address
}

output "private_ip" {
  value = google_compute_instance.this.network_interface[0].network_ip
}
