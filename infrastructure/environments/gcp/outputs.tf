output "handoff" {
  description = "비밀값 없는 인계 계약. 비밀값은 Secret Manager 참조로만 전달합니다."
  value = {
    schema_version = 1
    cloud          = "gcp"
    deployment_id  = var.deployment_id
    metadata       = module.metadata.common
    compute        = var.app == null ? null : local.managed_compute
    database       = { id = module.cloud_sql.instance_name, host = module.cloud_sql.private_ip, port = 5432, name = var.database_name, engine = "postgres", version = local.postgres_version, connection_name = module.cloud_sql.connection_name, app_db_user = module.cloud_sql.app_db_user, auth_mode = var.app == null ? "cloud-sql-iam-connector" : "password-secret-manager", public_ip = module.cloud_sql.public_ip, outgoing_ip = module.cloud_sql.outgoing_ip, tls_required = true, logical_decoding = true, admin_user = module.cloud_sql.admin_user, admin_secret_ref = google_secret_manager_secret.db_admin.id, app_user = var.app == null ? null : local.app_db_user, app_secret_ref = var.app == null ? null : google_secret_manager_secret.app_db.id }
    cache          = { id = module.memorystore.id, host = module.memorystore.host, port = module.memorystore.port, tls_required = local.redis_transit_encryption, auth_enabled = local.redis_auth, auth_source = local.redis_auth ? "memorystore-get-auth-string-via-authorized-bootstrap" : "none-vpc-only", server_ca_certs = module.memorystore.server_ca_certs }
    storage        = { id = module.storage.url, name = module.storage.bucket_name }
    network        = { id = module.network.network_id, subnetwork_id = module.network.subnetwork_id }
    app            = var.app == null ? null : local.managed_app_handoff
    router_inputs  = local.router_inputs
    pending_checks = var.app == null ? ["application-deployment", "database-grants-and-schema", "cache-auth-delivery", "replication-connectivity", "application-smoke-test"] : ["application-boot-log", "application-smoke-test"]
  }
}

locals {
  router_inputs = {
    name                          = "${var.deployment_id}-router"
    project_id                    = var.project_id
    region                        = var.region
    zone                          = var.zone
    network_name                  = module.network.network_name
    subnetwork_name               = module.network.subnetwork_name
    service_cidrs                 = ["${var.network.service_range_address}/${var.network.service_range_prefix}"]
    private_services_peering_name = module.network.private_services_peering_name
    client_service_account_emails = [module.iam.service_account_email]
    client_network_tags           = local.managed_app ? [module.app_cloud_run[0].network_tag] : []
    app_ports                     = var.app == null ? [] : [var.app.port]
    service_ports                 = [6432, 8080, 8000, 7000]
    redis_ports                   = [local.redis_transit_encryption ? 6378 : 6379]
    auth_key_secret_id            = try(element(reverse(split("/", var.tailscale.auth_key_secret)), 0), null)
  }
}
output "router_inputs" { value = local.router_inputs }

