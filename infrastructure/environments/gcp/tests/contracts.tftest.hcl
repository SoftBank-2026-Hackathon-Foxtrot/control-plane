mock_provider "google" {
  mock_resource "google_service_account" {
    defaults = { email = "test-app@test-project.iam.gserviceaccount.com" }
  }
  mock_resource "google_compute_network" {
    defaults = { id = "projects/test-project/global/networks/test" }
  }
  mock_resource "google_sql_database_instance" {
    defaults = { private_ip_address = "10.21.0.3", public_ip_address = "", connection_name = "test-project:asia-northeast3:test-db", ip_address = [] }
  }
  mock_resource "google_redis_instance" {
    defaults = { host = "10.21.1.4", port = 6379 }
  }
  mock_resource "google_secret_manager_secret" {
    defaults = { id = "projects/test-project/secrets/test" }
  }
  mock_resource "google_storage_hmac_key" {
    defaults = { access_id = "GOOGTESTACCESSID", secret = "TESTONLYhmacsecret" }
  }
}
run "root_wiring" {
  command = apply
  variables {
    deployment_id       = "fox-gcp-poc01"
    metadata            = { "project" = "foxtrot", "environment" = "poc", "owner" = "platform-team", "cost_center" = "hackathon", "workload" = "web", "migration_id" = "mig-001", "data_class" = "internal", "expires_on" = "2026-12-01" }
    region              = "asia-northeast3"
    zone                = "asia-northeast3-a"
    project_id          = "replace-with-project-id"
    bucket_name         = "replace-with-globally-unique-gcp-bucket"
    database_name       = "app"
    web_ingress_cidrs   = ["0.0.0.0/0"]
    web_ports           = [80, 443]
    admin_cidrs         = []
    deletion_protection = true
    app                 = { image = "asia-northeast3-docker.pkg.dev/test-project/apps/chiikawa-groupbuy:test", entrypoint = ["python", "-m", "app.main"] }
    network             = { "subnet_cidr" = "10.20.0.0/24", "service_range_address" = "10.21.0.0", "service_range_prefix" = 16 }
    sizing              = { "compute_machine_type" = "e2-small", "compute_disk_gib" = 30, "db_tier" = "db-custom-1-3840", "db_storage_gib" = 20, "db_max_storage_gib" = 100, "db_high_availability" = false, "cache_memory_gib" = 1, "cache_high_availability" = false }
  }
  assert {
    condition     = !contains(keys(module.app_runtime[0]), "script")
    error_message = "App schema creation and seed data belong to the user app, not IaC."
  }
  assert {
    condition     = output.handoff.cloud == "gcp" && output.handoff.schema_version == 1
    error_message = "GCP handoff contract must remain stable."
  }
  assert {
    condition     = output.handoff.compute.runtime == "CLOUD_RUN" && length(module.app_cloud_run) == 1
    error_message = "User apps must run on Cloud Run with Direct VPC egress, not an application VM."
  }
  assert {
    condition     = length(var.app.entrypoint) > 0
    error_message = "The platform supplies the application container entrypoint."
  }
  assert {
    condition     = output.handoff.database.version == "17" && output.handoff.database.logical_decoding && !output.handoff.cache.tls_required
    error_message = "PG17 with logical decoding and a plain-RESP cache are the defaults."
  }
}
run "reject_open_ssh" {
  command = plan
  variables {
    deployment_id       = "fox-gcp-poc01"
    metadata            = { "project" = "foxtrot", "environment" = "poc", "owner" = "platform-team", "cost_center" = "hackathon", "workload" = "web", "migration_id" = "mig-001", "data_class" = "internal", "expires_on" = "2026-12-01" }
    region              = "asia-northeast3"
    zone                = "asia-northeast3-a"
    project_id          = "replace-with-project-id"
    bucket_name         = "replace-with-globally-unique-gcp-bucket"
    database_name       = "app"
    web_ingress_cidrs   = ["0.0.0.0/0"]
    web_ports           = [80, 443]
    admin_cidrs         = ["0.0.0.0/0"]
    deletion_protection = true
    app                 = { image = "asia-northeast3-docker.pkg.dev/test-project/apps/chiikawa-groupbuy:test", entrypoint = ["python", "-m", "app.main"] }
    network             = { "subnet_cidr" = "10.20.0.0/24", "service_range_address" = "10.21.0.0", "service_range_prefix" = 16 }
    sizing              = { "compute_machine_type" = "e2-small", "compute_disk_gib" = 30, "db_tier" = "db-custom-1-3840", "db_storage_gib" = 20, "db_max_storage_gib" = 100, "db_high_availability" = false, "cache_memory_gib" = 1, "cache_high_availability" = false }
  }
  expect_failures = [var.admin_cidrs]
}
run "reject_database_public_port" {
  command = plan
  variables {
    deployment_id       = "fox-gcp-poc01"
    metadata            = { "project" = "foxtrot", "environment" = "poc", "owner" = "platform-team", "cost_center" = "hackathon", "workload" = "web", "migration_id" = "mig-001", "data_class" = "internal", "expires_on" = "2026-12-01" }
    region              = "asia-northeast3"
    zone                = "asia-northeast3-a"
    project_id          = "replace-with-project-id"
    bucket_name         = "replace-with-globally-unique-gcp-bucket"
    database_name       = "app"
    web_ingress_cidrs   = ["0.0.0.0/0"]
    web_ports           = [5432]
    admin_cidrs         = []
    deletion_protection = true
    app                 = { image = "asia-northeast3-docker.pkg.dev/test-project/apps/chiikawa-groupbuy:test", entrypoint = ["python", "-m", "app.main"] }
    network             = { "subnet_cidr" = "10.20.0.0/24", "service_range_address" = "10.21.0.0", "service_range_prefix" = 16 }
    sizing              = { "compute_machine_type" = "e2-small", "compute_disk_gib" = 30, "db_tier" = "db-custom-1-3840", "db_storage_gib" = 20, "db_max_storage_gib" = 100, "db_high_availability" = false, "cache_memory_gib" = 1, "cache_high_availability" = false }
  }
  expect_failures = [var.web_ports]
}
run "reject_unreviewed_size" {
  command = plan
  variables {
    deployment_id       = "fox-gcp-poc01"
    metadata            = { "project" = "foxtrot", "environment" = "poc", "owner" = "platform-team", "cost_center" = "hackathon", "workload" = "web", "migration_id" = "mig-001", "data_class" = "internal", "expires_on" = "2026-12-01" }
    region              = "asia-northeast3"
    zone                = "asia-northeast3-a"
    project_id          = "replace-with-project-id"
    bucket_name         = "replace-with-globally-unique-gcp-bucket"
    database_name       = "app"
    web_ingress_cidrs   = ["0.0.0.0/0"]
    web_ports           = [80, 443]
    admin_cidrs         = []
    deletion_protection = true
    app                 = { image = "asia-northeast3-docker.pkg.dev/test-project/apps/chiikawa-groupbuy:test", entrypoint = ["python", "-m", "app.main"] }
    network             = { "subnet_cidr" = "10.20.0.0/24", "service_range_address" = "10.21.0.0", "service_range_prefix" = 16 }
    sizing              = { "compute_machine_type" = "a3-highgpu-8g" }
  }
  expect_failures = [var.sizing]
}
run "private_database" {
  command = plan
  module { source = "../../modules/gcp/cloud_sql" }
  variables {
    name                = "test-db"
    labels              = { "project" = "foxtrot", "environment" = "poc", "owner" = "platform-team", "cost_center" = "hackathon", "workload" = "web", "migration_id" = "mig-001", "data_class" = "internal", "expires_on" = "2026-12-01", "deployment_id" = "test-poc", "managed_by" = "terraform", "template_version" = "v0-1", "security_profile" = "public-app-private-data-v1" }
    region              = "asia-northeast3"
    network_id          = "projects/test-project/global/networks/test"
    service_range_name  = "services"
    tier                = "db-custom-1-3840"
    storage_gib         = 20
    max_storage_gib     = 100
    high_availability   = false
    database_name       = "app"
    app_service_account = "app@test-project.iam.gserviceaccount.com"
    deletion_protection = true
    admin_password      = "TESTONLYadminpassword"
  }
  assert {
    condition     = google_sql_database_instance.this.database_version == "POSTGRES_17" && { for f in google_sql_database_instance.this.settings[0].database_flags : f.name => f.value }["cloudsql.logical_decoding"] == "on"
    error_message = "PostgreSQL 17 with logical decoding is required by the DB migration agent."
  }
  assert {
    condition     = !google_sql_database_instance.this.settings[0].ip_configuration[0].ipv4_enabled && google_sql_database_instance.this.settings[0].ip_configuration[0].ssl_mode == "ENCRYPTED_ONLY"
    error_message = "Cloud SQL must stay private with TLS."
  }
  assert {
    condition     = google_sql_database_instance.this.deletion_protection && google_sql_database_instance.this.settings[0].backup_configuration[0].point_in_time_recovery_enabled
    error_message = "Deletion protection and PITR are required."
  }
}
run "private_storage" {
  command = plan
  module { source = "../../modules/gcp/storage" }
  variables {
    name   = "test-poc-bucket"
    labels = { "project" = "foxtrot", "environment" = "poc", "owner" = "platform-team", "cost_center" = "hackathon", "workload" = "web", "migration_id" = "mig-001", "data_class" = "internal", "expires_on" = "2026-12-01", "deployment_id" = "test-poc", "managed_by" = "terraform", "template_version" = "v0-1", "security_profile" = "public-app-private-data-v1" }
    region = "asia-northeast3"
  }
  assert {
    condition     = google_storage_bucket.this.public_access_prevention == "enforced" && google_storage_bucket.this.uniform_bucket_level_access
    error_message = "Bucket must block public access and object ACLs."
  }
  assert {
    condition     = !google_storage_bucket.this.force_destroy && google_storage_bucket.this.versioning[0].enabled
    error_message = "Versioning and nonempty-bucket deletion protection are required."
  }
}
run "cache_plain_resp_by_default" {
  command = plan
  module { source = "../../modules/gcp/memorystore" }
  variables {
    name               = "test-cache"
    labels             = { "project" = "foxtrot", "environment" = "poc", "owner" = "platform-team", "cost_center" = "hackathon", "workload" = "web", "migration_id" = "mig-001", "data_class" = "internal", "expires_on" = "2026-12-01", "deployment_id" = "test-poc", "managed_by" = "terraform", "template_version" = "v0-1", "security_profile" = "public-app-private-data-v1" }
    region             = "asia-northeast3"
    network_id         = "projects/test-project/global/networks/test"
    service_range_name = "services"
    memory_gib         = 1
    high_availability  = false
  }
  assert {
    condition     = !google_redis_instance.this.auth_enabled && google_redis_instance.this.transit_encryption_mode == "DISABLED"
    error_message = "The team's Redis proxy speaks plain RESP to its backends."
  }
  assert {
    condition     = google_redis_instance.this.redis_configs == tomap({ "notify-keyspace-events" = "KEA", "maxmemory-policy" = "noeviction" })
    error_message = "Migration requires keyspace notifications and noeviction."
  }

}

run "management_enabled" {
  command = plan
  module { source = "../../modules/gcp/firewall" }
  variables {
    name                     = "test-poc"
    web_ingress_cidrs        = ["0.0.0.0/0"]
    web_ports                = [80, 443]
    admin_cidrs              = []
    labels                   = {}
    network_id               = "test-network"
    app_service_account      = "app@test.iam.gserviceaccount.com"
    service_cidr             = "10.31.0.0/16"
    proxy_cidrs              = ["10.50.0.10/32"]
    enable_cloudflare_tunnel = true
  }
  assert {
    condition     = google_compute_firewall.proxy[0].destination_ranges == toset(["10.50.0.10/32"]) && one(google_compute_firewall.proxy[0].allow).ports == tolist(["6379", "6432"]) && google_compute_firewall.proxy[0].priority < google_compute_firewall.deny_other_egress.priority && length(google_compute_firewall.cloudflare_tunnel[0].allow) == 2
    error_message = "Management paths must be explicit, port-scoped and opt-in."
  }
}

run "management_default_closed" {
  command = plan
  module { source = "../../modules/gcp/firewall" }
  variables {
    name                = "test-poc"
    web_ingress_cidrs   = ["0.0.0.0/0"]
    web_ports           = [80, 443]
    admin_cidrs         = []
    labels              = {}
    network_id          = "test-network"
    app_service_account = "app@test.iam.gserviceaccount.com"
    service_cidr        = "10.31.0.0/16"
  }
  assert {
    condition     = length(google_compute_firewall.proxy) == 0 && length(google_compute_firewall.cloudflare_tunnel) == 0
    error_message = "Management paths must be explicit, port-scoped and opt-in."
  }
}

run "management_reject_public" {
  command = plan
  module { source = "../../modules/gcp/firewall" }
  variables {
    name                     = "test-poc"
    web_ingress_cidrs        = ["0.0.0.0/0"]
    web_ports                = [80, 443]
    admin_cidrs              = []
    labels                   = {}
    network_id               = "test-network"
    app_service_account      = "app@test.iam.gserviceaccount.com"
    service_cidr             = "10.31.0.0/16"
    proxy_cidrs              = ["0.0.0.0/0"]
    enable_cloudflare_tunnel = true
  }
  expect_failures = [var.proxy_cidrs]
}

run "legacy_infrastructure_only" {
  command = apply
  variables {
    deployment_id       = "fox-gcp-poc01"
    metadata            = { "project" = "foxtrot", "environment" = "poc", "owner" = "platform-team", "cost_center" = "hackathon", "workload" = "web", "migration_id" = "mig-001", "data_class" = "internal", "expires_on" = "2026-12-01" }
    region              = "asia-northeast3"
    zone                = "asia-northeast3-a"
    project_id          = "replace-with-project-id"
    bucket_name         = "replace-with-globally-unique-gcp-bucket"
    database_name       = "app"
    web_ingress_cidrs   = ["0.0.0.0/0"]
    web_ports           = [80, 443]
    admin_cidrs         = []
    deletion_protection = true
    network             = { "subnet_cidr" = "10.20.0.0/24", "service_range_address" = "10.21.0.0", "service_range_prefix" = 16 }
    sizing              = { "compute_machine_type" = "e2-small", "compute_disk_gib" = 30, "db_tier" = "db-custom-1-3840", "db_storage_gib" = 20, "db_max_storage_gib" = 100, "db_high_availability" = false, "cache_memory_gib" = 1, "cache_high_availability" = false }
  }
  assert {
    condition     = output.handoff.app == null && output.handoff.cache.tls_required && output.handoff.database.version == "16"
    error_message = "Legacy inputs retain infrastructure-only deployment, PG16 and cache TLS."
  }
}
