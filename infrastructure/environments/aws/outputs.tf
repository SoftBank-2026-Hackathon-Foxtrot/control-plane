output "handoff" {
  description = "비밀값 없는 모듈 인계 계약. resource_created는 앱/DB 이전 readiness를 보장하지 않습니다."
  value = {
    schema_version = 1
    cloud          = "aws"
    deployment_id  = var.deployment_id
    metadata       = module.metadata.common
    compute        = var.app == null ? null : local.managed_compute
    database       = { id = module.rds.identifier, host = module.rds.address, port = module.rds.port, name = var.database_name, engine = "postgres", version = local.postgres_version, tls_required = true, logical_replication = true, publicly_accessible = length(var.replication_cidrs) > 0, admin_secret_ref = module.rds.master_secret_arn, app_user = var.app == null ? null : local.app_db_user, app_secret_ref = var.app == null ? null : aws_secretsmanager_secret.app_db.arn, arn = module.rds.arn, operator_secret_ref = aws_secretsmanager_secret.db_operator.arn }
    cache          = { id = module.elasticache.id, host = module.elasticache.endpoint, port = module.elasticache.port, tls_required = local.redis_transit_encryption, auth_source = local.redis_transit_encryption ? "external-secret-injection" : "none-security-group-only" }
    storage        = { id = module.s3.bucket_arn, name = module.s3.bucket_id }
    network        = { id = module.vpc.vpc_id, app_security_group_id = module.security_groups.app_id }
    app            = var.app == null ? null : local.managed_app_handoff
    router_inputs  = local.router_inputs
    pending_checks = var.app == null ? ["application-deployment", "database-grants-and-schema", "replication-connectivity", "application-smoke-test"] : ["application-boot-log", "application-smoke-test"]
  }
}

locals {
  router_inputs = {
    name                        = "${var.deployment_id}-router"
    region                      = var.region
    account_id                  = var.account_id
    vpc_id                      = module.vpc.vpc_id
    subnet_id                   = module.vpc.public_subnet_id
    route_table_ids             = module.vpc.route_table_ids
    database_security_group_ids = [module.security_groups.db_id]
    cache_security_group_ids    = [module.security_groups.cache_id]
    client_security_group_ids   = [module.security_groups.app_id]
    app_security_group_ids      = [module.security_groups.app_id]
    app_ports                   = var.app == null ? [] : [var.app.port]
    service_ports               = [6432, 8080, 8000, 7000]
    redis_ports                 = [6379]
    auth_key_secret_arn         = try(var.tailscale.auth_key_secret, null)
  }
}
output "router_inputs" { value = local.router_inputs }

