module "metadata" {
  source        = "../../modules/metadata"
  deployment_id = var.deployment_id
  metadata      = var.metadata
}
module "network" {
  source                = "../../modules/gcp/network"
  name                  = var.deployment_id
  labels                = merge(module.metadata.common, { component = "network" })
  region                = var.region
  subnet_cidr           = var.network.subnet_cidr
  service_range_address = var.network.service_range_address
  service_range_prefix  = var.network.service_range_prefix
  depends_on            = [google_project_service.required]
}
module "storage" {
  source        = "../../modules/gcp/storage"
  name          = var.bucket_name
  force_destroy = var.force_destroy_storage
  labels        = merge(module.metadata.common, { component = "storage" })
  region        = var.region
  depends_on    = [google_project_service.required]
}
module "iam" {
  source      = "../../modules/gcp/iam"
  name        = var.deployment_id
  labels      = merge(module.metadata.common, { component = "identity" })
  project_id  = var.project_id
  bucket_name = module.storage.bucket_name
  depends_on  = [google_project_service.required]
}
module "firewall" {
  source                   = "../../modules/gcp/firewall"
  name                     = var.deployment_id
  labels                   = merge(module.metadata.common, { component = "network" })
  network_id               = module.network.network_id
  app_service_account      = module.iam.service_account_email
  web_ingress_cidrs        = var.web_ingress_cidrs
  web_ports                = var.web_ports
  admin_cidrs              = var.admin_cidrs
  proxy_cidrs              = var.proxy_cidrs
  enable_cloudflare_tunnel = var.enable_cloudflare_tunnel
  enable_tailscale         = false
  service_cidr             = "${var.network.service_range_address}/${var.network.service_range_prefix}"
}
module "cloud_sql" {
  source              = "../../modules/gcp/cloud_sql"
  name                = "${var.deployment_id}-db"
  labels              = merge(module.metadata.common, { component = "database" })
  region              = var.region
  network_id          = module.network.network_id
  service_range_name  = module.network.service_range_name
  tier                = var.sizing.db_tier
  storage_gib         = var.sizing.db_storage_gib
  max_storage_gib     = var.sizing.db_max_storage_gib
  high_availability   = var.sizing.db_high_availability
  database_name       = var.database_name
  app_service_account = module.iam.service_account_email
  deletion_protection = var.deletion_protection
  admin_password      = random_password.db_admin.result
  postgres_version    = local.postgres_version
  authorized_networks = var.db_authorized_networks
  depends_on          = [module.network]
}
module "memorystore" {
  source             = "../../modules/gcp/memorystore"
  name               = "${var.deployment_id}-cache"
  labels             = merge(module.metadata.common, { component = "cache" })
  region             = var.region
  network_id         = module.network.network_id
  service_range_name = module.network.service_range_name
  memory_gib         = var.sizing.cache_memory_gib
  high_availability  = var.sizing.cache_high_availability
  auth_enabled       = local.redis_auth
  transit_encryption = local.redis_transit_encryption
  depends_on         = [module.network]
}
