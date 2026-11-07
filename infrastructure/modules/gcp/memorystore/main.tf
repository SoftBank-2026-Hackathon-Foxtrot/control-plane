resource "google_redis_instance" "this" {
  name           = var.name
  region         = var.region
  tier           = var.high_availability ? "STANDARD_HA" : "BASIC"
  memory_size_gb = var.memory_gib
  redis_version  = "REDIS_7_0"
  redis_configs = {
    "notify-keyspace-events" = "KEA"
    "maxmemory-policy"       = "noeviction"
  }
  authorized_network = var.network_id
  connect_mode       = "PRIVATE_SERVICE_ACCESS"
  reserved_ip_range  = var.service_range_name
  # The team's Redis proxy (redis-migrator) speaks plain RESP to its backends, so TLS and AUTH
  # are off by default; only this VPC (and peered paths) can reach the instance.
  auth_enabled            = var.auth_enabled
  transit_encryption_mode = var.transit_encryption ? "SERVER_AUTHENTICATION" : "DISABLED"
  labels                  = var.labels
}
