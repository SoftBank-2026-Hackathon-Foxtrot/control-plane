# Optional app-owned runtimes. Existing shared services stay unchanged during adoption.
# The platform registers these addresses for the corresponding application; no worker pool.
variable "application_workers" {
  type = map(object({
    db_api_token_secret_arn    = string
    redis_api_token_secret_arn = string
    redis_clients_secret_arn   = string
    database_secret_arns       = list(string)
    redis_secret_arns          = list(string)
  }))
  default = {}
  validation {
    condition     = alltrue([for id in keys(var.application_workers) : can(regex("^[a-z][a-z0-9-]{0,23}$", id))])
    error_message = "Application IDs must be lowercase slugs of at most 24 characters."
  }
  validation {
    condition = length(distinct(flatten([for app in values(var.application_workers) : [
      app.db_api_token_secret_arn, app.redis_api_token_secret_arn
    ]]))) == 2 * length(var.application_workers)
    error_message = "Each application adapter must use its own API token secret."
  }
}

module "application_workers" {
  for_each                   = var.application_workers
  source                     = "../../../modules/app_migration_worker"
  app_id                     = each.key
  cluster_id                 = local.svc.cluster_id
  subnet_ids                 = local.svc.subnet_ids
  security_group_ids         = local.svc.security_group_ids
  namespace_id               = local.svc.namespace_id
  log_group                  = local.svc.log_group
  region                     = local.svc.region
  db_image                   = local.image["db-migration-adaptor"]
  redis_data_image           = local.image["redis-proxy"]
  redis_api_image            = local.image["redis-statemig"]
  db_state_table             = { name = aws_dynamodb_table.db_operator.name, arn = aws_dynamodb_table.db_operator.arn }
  state_file_system_id       = aws_efs_file_system.state.id
  db_runtime_secrets         = local.db_secrets
  db_backend_secrets         = { DB_OPERATOR_RUNTIME_GCP_CREDENTIALS_JSON = aws_secretsmanager_secret.gcp["db"].arn }
  db_api_token_secret_arn    = each.value.db_api_token_secret_arn
  redis_api_token_secret_arn = each.value.redis_api_token_secret_arn
  redis_clients_secret_arn   = each.value.redis_clients_secret_arn
  database_secret_arns       = each.value.database_secret_arns
  redis_secret_arns          = each.value.redis_secret_arns
  environment                = local.otel_env
  telemetry_secrets          = local.otel_secrets
  depends_on                 = [aws_efs_mount_target.state, aws_secretsmanager_secret_version.db_runtime]
}

locals {
  configured_seed = yamldecode(file("${path.module}/seed.yaml"))
  control_workers = try(module.application_workers[local.configured_seed.application].application, null)
  control_seed_yaml = local.control_workers == null ? file("${path.module}/seed.yaml") : yamlencode(merge(local.configured_seed, {
    adapters = merge(local.configured_seed.adapters, {
      database = merge(local.configured_seed.adapters.database, { url = local.control_workers.database.url })
      redis    = merge(local.configured_seed.adapters.redis, { url = local.control_workers.redis.url })
    })
  }))
  control_db_namespace = local.control_workers == null ? local.db_namespace : local.control_workers.dbNamespace
  control_db_token     = local.control_workers == null ? local.secret_arn["db-adapter-token"] : local.control_workers.database.tokenSecret
  control_redis_token  = local.control_workers == null ? local.secret_arn["redis-admin-token"] : local.control_workers.redis.tokenSecret
}

output "application_workers" {
  value = { for app, workers in module.application_workers : app => workers.application }
}
