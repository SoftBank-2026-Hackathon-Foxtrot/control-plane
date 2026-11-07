output "id" {
  value = google_redis_instance.this.id
}

output "host" {
  value = google_redis_instance.this.host
}

output "port" {
  value = google_redis_instance.this.port
}

output "server_ca_certs" {
  value = google_redis_instance.this.server_ca_certs
}
