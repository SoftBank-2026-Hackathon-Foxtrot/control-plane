module "metadata" {
  source        = "../../modules/metadata"
  deployment_id = var.deployment_id
  metadata      = var.metadata
}
module "vpc" {
  source               = "../../modules/aws/vpc"
  name                 = var.deployment_id
  tags                 = merge(module.metadata.common, { component = "network" })
  vpc_cidr             = var.network.vpc_cidr
  public_subnet_cidr   = var.network.public_subnet_cidr
  private_subnet_cidrs = var.network.private_subnet_cidrs
  availability_zones   = var.network.availability_zones
  # A database reachable from another cloud needs a route out of its subnets.
  data_subnets_internet_route = length(var.replication_cidrs) > 0 && !local.managed_app
}
module "security_groups" {
  source                   = "../../modules/aws/security_groups"
  name                     = var.deployment_id
  tags                     = merge(module.metadata.common, { component = "network" })
  vpc_id                   = module.vpc.vpc_id
  web_ingress_cidrs        = var.web_ingress_cidrs
  web_ports                = var.web_ports
  admin_cidrs              = var.admin_cidrs
  proxy_cidrs              = var.proxy_cidrs
  enable_cloudflare_tunnel = var.enable_cloudflare_tunnel || try(var.app.tunnel_secret, null) != null
  migration_source_cidrs   = var.migration_source_cidrs
  replication_cidrs        = var.replication_cidrs
  enable_tailscale         = var.tailscale != null
}
module "s3" {
  source        = "../../modules/aws/s3"
  bucket_name   = var.bucket_name
  force_destroy = var.force_destroy_storage
  tags          = merge(module.metadata.common, { component = "storage" })
}
module "rds" {
  source                    = "../../modules/aws/rds"
  name                      = "${var.deployment_id}-db"
  tags                      = merge(module.metadata.common, { component = "database" })
  subnet_ids                = module.vpc.private_subnet_ids
  security_group_id         = module.security_groups.db_id
  instance_class            = var.sizing.db_instance_class
  storage_gib               = var.sizing.db_storage_gib
  max_storage_gib           = var.sizing.db_max_storage_gib
  multi_az                  = var.sizing.db_multi_az
  engine_version            = local.postgres_version
  database_name             = var.database_name
  deletion_protection       = var.deletion_protection
  final_snapshot_identifier = var.final_snapshot_identifier
  publicly_accessible       = length(var.replication_cidrs) > 0
}
module "elasticache" {
  source             = "../../modules/aws/elasticache"
  name               = "${var.deployment_id}-cache"
  tags               = merge(module.metadata.common, { component = "cache" })
  subnet_ids         = module.vpc.private_subnet_ids
  security_group_id  = module.security_groups.cache_id
  node_type          = var.sizing.cache_node_type
  node_count         = var.sizing.cache_node_count
  transit_encryption = local.redis_transit_encryption
  auth_token         = var.redis_auth_token
}
