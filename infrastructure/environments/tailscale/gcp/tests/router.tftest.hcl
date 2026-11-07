mock_provider "google" {
  mock_data "google_compute_network" {
    defaults = { name = "foxtrot", self_link = "https://www.googleapis.com/compute/v1/projects/test-project/global/networks/foxtrot" }
  }
  mock_data "google_compute_subnetwork" {
    defaults = {
      ip_cidr_range = "10.40.0.0/24"
      network       = "https://www.googleapis.com/compute/v1/projects/test-project/global/networks/foxtrot"
      self_link     = "https://www.googleapis.com/compute/v1/projects/test-project/regions/asia-northeast3/subnetworks/foxtrot"
    }
  }
}
variables {
  client_service_account_emails         = ["app@test-project.iam.gserviceaccount.com"]
  project_id                            = "test-project"
  network_name                          = "foxtrot"
  subnetwork_name                       = "foxtrot"
  service_cidrs                         = ["10.60.0.0/16"]
  private_services_peering_name         = "servicenetworking-googleapis-com"
  private_services_import_custom_routes = false
  remote_cidrs                          = ["10.20.0.0/16"]
  auth_key_secret_id                    = "tailscale-test"
}
run "shared_router_and_cloudsql_return_route" {
  command = plan
  assert {
    condition     = google_compute_instance.router.can_ip_forward && length(google_compute_route.remote) == 1
    error_message = "GCE must forward packets and route remote DB/worker subnets."
  }
  assert {
    condition     = google_compute_network_peering_routes_config.private_services[0].export_custom_routes && contains(output.advertised_cidrs, "10.60.0.0/16")
    error_message = "Cloud SQL needs its PSA range advertised and exported return routes."
  }
  assert {
    condition     = google_secret_manager_secret_iam_member.auth_key.secret_id == var.auth_key_secret_id
    error_message = "The VM service account must have access only to the configured auth secret."
  }
  assert {
    condition     = google_compute_firewall.remote_clients[0].direction == "EGRESS" && google_compute_firewall.remote_clients[0].priority < 2000
    error_message = "Existing app deny rules must permit the explicitly selected remote DB destinations."
  }
}
run "reject_missing_cloudsql_return_config" {
  command = plan
  variables { private_services_peering_name = null }
  expect_failures = [var.service_cidrs]
}
run "reject_implicit_peering_import_change" {
  command = plan
  variables { private_services_import_custom_routes = null }
  expect_failures = [google_compute_network_peering_routes_config.private_services]
}
run "redis_uses_shared_router_without_opening_db_ports" {
  command = plan
  variables {
    redis_ports           = [6378, 6379]
    cache_network_tags    = ["redis"]
    database_network_tags = ["postgres"]
  }
  assert {
    condition     = toset(one(google_compute_firewall.remote_clients[0].allow).ports) == toset(["5432", "6378", "6379"]) && toset(one(google_compute_firewall.clients.allow).ports) == toset(["5432", "6378", "6379"])
    error_message = "Redis and PostgreSQL must share the same router and restricted-client egress."
  }
  assert {
    condition     = toset(one(google_compute_firewall.cache_clients["redis"].allow).ports) == toset(["6378", "6379"]) && toset(one(google_compute_firewall.database_clients["postgres"].allow).ports) == toset(["5432"])
    error_message = "Cache and PostgreSQL VM ingress must remain separate."
  }
}
run "reject_unselected_cache_ports" {
  command = plan
  variables { cache_network_tags = ["redis"] }
  expect_failures = [var.cache_network_tags]
}
run "reject_non_redis_port" {
  command = plan
  variables { redis_ports = [22] }
  expect_failures = [var.redis_ports]
}

run "app_readiness_uses_private_routes" {
  command = plan
  variables {
    app_ports     = [8000]
    service_ports = [6432, 8080]
  }
  assert {
    condition     = google_compute_firewall.remote_app[0].source_ranges == var.remote_cidrs && one(google_compute_firewall.remote_app[0].allow).ports == tolist(["8000"])
    error_message = "App readiness ingress must use the private source ranges and selected app port."
  }
  assert {
    condition     = contains(one(google_compute_firewall.remote_clients[0].allow).ports, "6432")
    error_message = "Apps must be able to reach the platform PgBouncer through the shared router."
  }
}
