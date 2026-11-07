module "app_cloud_run" {
  count                 = local.managed_app ? 1 : 0
  source                = "../../modules/gcp/app_cloud_run"
  name                  = var.deployment_id
  region                = var.region
  network_id            = module.network.network_id
  subnetwork_id         = module.network.subnetwork_id
  service_account       = module.iam.service_account_email
  labels                = merge(module.metadata.common, { component = "compute" })
  app                   = var.app
  environment           = module.app_runtime[0].environment
  bootstrap_environment = module.app_runtime[0].bootstrap_environment
  entrypoint            = var.app.entrypoint
  runtime_secrets = {
    APP_DB_PASSWORD    = google_secret_manager_secret.app_db.id
    DB_ADMIN_PASSWORD  = google_secret_manager_secret.db_admin.id
    APP_STORAGE_SECRET = google_secret_manager_secret.storage.id
  }
  proxy_cidrs  = var.proxy_cidrs
  service_cidr = "${var.network.service_range_address}/${var.network.service_range_prefix}"
  depends_on = [google_project_service.required, module.cloud_sql,
    google_secret_manager_secret_iam_member.app, google_secret_manager_secret_version.app_db,
  google_secret_manager_secret_version.db_admin, google_secret_manager_secret_version.storage]
}
locals {
  managed_compute = {
    id            = try(module.app_cloud_run[0].service_id, null)
    public_ip     = null
    private_ip    = null
    identity      = module.iam.service_account_email
    runtime       = "CLOUD_RUN"
    readiness_url = try("${module.app_cloud_run[0].url}${var.app.health_path}", null)
  }
  managed_app_handoff = {
    image       = try(var.app.image, null)
    url         = try(module.app_cloud_run[0].url, null)
    env_keys    = try(module.app_runtime[0].app_env_keys, [])
    boot_log    = "run.googleapis.com/stdout"
    health_path = try(var.app.health_path, null)
    runtime     = "CLOUD_RUN"
  }
}
