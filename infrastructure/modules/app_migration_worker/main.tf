# One application owns these two permanent tasks. FINALIZE never scales them down.
# Connection endpoints arrive through the adapter APIs, not task environment variables.
terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

variable "app_id" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,23}$", var.app_id))
    error_message = "app_id must be a lowercase slug of at most 24 characters."
  }
}
variable "cluster_id" { type = string }
variable "subnet_ids" { type = list(string) }
variable "security_group_ids" { type = list(string) }
variable "namespace_id" { type = string }
variable "log_group" { type = string }
variable "region" { type = string }
variable "db_image" { type = string }
variable "redis_data_image" { type = string }
variable "redis_api_image" { type = string }
variable "db_state_table" {
  type = object({ name = string, arn = string })
}
variable "state_file_system_id" { type = string }
variable "db_runtime_secrets" {
  description = "Existing PgBouncer runtime secret environment names mapped to secret ARNs."
  type        = map(string)
}
variable "db_backend_secrets" {
  description = "Provider credentials used by the API/executor only, never by PgBouncer."
  type        = map(string)
  default     = {}
}
variable "db_api_token_secret_arn" { type = string }
variable "redis_api_token_secret_arn" { type = string }
variable "redis_clients_secret_arn" { type = string }
variable "database_secret_arns" { type = list(string) }
variable "redis_secret_arns" { type = list(string) }
variable "environment" {
  type    = map(string)
  default = {}
}
variable "telemetry_secrets" {
  type    = map(string)
  default = {}
}

locals {
  db_name      = "db-${var.app_id}"
  redis_name   = "redis-${var.app_id}"
  db_namespace = "app:${var.app_id}"
  db_env = merge(var.environment, {
    DB_OPERATOR_STATE_TABLE        = var.db_state_table.name
    DB_OPERATOR_STATE_NAMESPACE    = local.db_namespace
    AWS_DEFAULT_REGION             = var.region
    DB_OPERATOR_REGISTRY           = "/run/foxtrot/registry.json"
    DB_OPERATOR_REGISTRY_JSON      = jsonencode({ databases = {}, bindings = {}, workloads = {}, tables = [], sequences = [] })
    DB_OPERATOR_BINDINGS_ENABLED   = "true"
    DB_OPERATOR_POOLER_DIRECTORY   = "/run/foxtrot"
    DB_OPERATOR_PUBLIC_HOST        = "${local.db_name}.internal"
    DB_OPERATOR_PUBLIC_PORT        = "6432"
    DB_OPERATOR_RUNTIME_CLIENT_TLS = "require"
    DB_OPERATOR_RUNTIME_SERVER_TLS = "verify-full"
    DB_OPERATOR_RUNTIME_ADMIN_USER = "pooler_admin"
  })
  db_secrets = merge(var.db_runtime_secrets, var.telemetry_secrets, {
    DB_OPERATOR_API_TOKEN = var.db_api_token_secret_arn
  })
  redis_env = merge(var.environment, {
    AWS_DEFAULT_REGION         = var.region
    STATEMIG_CONTROL_FILE      = "/state/control.json"
    STATEMIG_PROXY             = "http://127.0.0.1:7000"
    STATEMIG_SYNCWORKER        = "http://127.0.0.1:7100"
    STATEMIG_PROXY_PUBLIC_HOST = "${local.redis_name}.internal"
    STATEMIG_PROXY_PUBLIC_PORT = "6379"
  })
  redis_secrets = { STATEMIG_ADMIN_TOKEN = var.redis_api_token_secret_arn }
}

resource "aws_efs_access_point" "redis" {
  file_system_id = var.state_file_system_id
  posix_user {
    uid = 0
    gid = 0
  }
  root_directory {
    path = "/applications/${var.app_id}/redis"
    creation_info {
      owner_uid   = 0
      owner_gid   = 0
      permissions = "0700"
    }
  }
  tags = { application = var.app_id }
}

module "database" {
  source             = "../platform_service"
  name               = local.db_name
  cluster_id         = var.cluster_id
  subnet_ids         = var.subnet_ids
  security_group_ids = var.security_group_ids
  namespace_id       = var.namespace_id
  log_group          = var.log_group
  region             = var.region
  cpu                = 512
  memory             = 1024
  desired_count      = 1
  bind_volumes       = ["runtime"]
  containers = [
    {
      name        = "pooler"
      image       = var.db_image
      entryPoint  = ["foxtrot-db-runtime"]
      command     = ["pooler"]
      ports       = [6432]
      environment = local.db_env
      secrets     = local.db_secrets
      mounts      = { runtime = "/run/foxtrot" }
      health      = ["test -f /run/foxtrot/pooler.ready && pg_isready -q -h 127.0.0.1 -p 6432 -d pgbouncer -U pooler_admin -t 3"]
    },
    {
      name        = "backend"
      image       = var.db_image
      entryPoint  = ["foxtrot-db-runtime"]
      command     = ["backend"]
      ports       = [8080]
      environment = local.db_env
      secrets     = merge(local.db_secrets, var.db_backend_secrets)
      mounts      = { runtime = "/run/foxtrot" }
      health      = ["curl --insecure --fail --silent --max-time 3 https://127.0.0.1:8080/live"]
      dependsOn   = [{ containerName = "pooler", condition = "HEALTHY" }]
    },
  ]
  task_policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      { Effect = "Allow", Action = ["dynamodb:DescribeTable"], Resource = var.db_state_table.arn },
      {
        Effect   = "Allow"
        Resource = var.db_state_table.arn
        Action = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem",
        "dynamodb:DeleteItem", "dynamodb:ConditionCheckItem"]
        Condition = {
          "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = [local.db_namespace, "${local.db_namespace}:control"] }
          "Null"                      = { "dynamodb:LeadingKeys" = "false" }
        }
      },
      ], length(var.database_secret_arns) == 0 ? [] : [
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = var.database_secret_arns }
    ])
  })
}

module "redis" {
  source             = "../platform_service"
  name               = local.redis_name
  cluster_id         = var.cluster_id
  subnet_ids         = var.subnet_ids
  security_group_ids = var.security_group_ids
  namespace_id       = var.namespace_id
  log_group          = var.log_group
  region             = var.region
  cpu                = 1024
  memory             = 2048
  desired_count      = 1
  efs = { state = {
    file_system_id  = var.state_file_system_id
    access_point_id = aws_efs_access_point.redis.id
  } }
  containers = [
    {
      name        = "proxy"
      image       = var.redis_data_image
      entryPoint  = ["statemig-proxy"]
      ports       = [6379]
      environment = merge(local.redis_env, { STATEMIG_ADMIN = "127.0.0.1:7000" })
      secrets     = merge(local.redis_secrets, { STATEMIG_CLIENTS = var.redis_clients_secret_arn })
      mounts      = { state = "/state" }
    },
    {
      name        = "syncworker"
      image       = var.redis_data_image
      entryPoint  = ["statemig-syncworker"]
      environment = { STATEMIG_ADMIN = "127.0.0.1:7100" }
      secrets     = local.redis_secrets
    },
    {
      name        = "api"
      image       = var.redis_api_image
      command     = ["uvicorn", "statemig.api:app", "--host", "0.0.0.0", "--port", "8090", "--timeout-keep-alive", "120"]
      ports       = [8090]
      environment = local.redis_env
      secrets     = merge(local.redis_secrets, var.telemetry_secrets)
      mounts      = { state = "/state" }
      dependsOn   = [{ containerName = "proxy", condition = "START" }, { containerName = "syncworker", condition = "START" }]
    },
  ]
  task_policy = length(var.redis_secret_arns) == 0 ? null : jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = var.redis_secret_arns }]
  })
}

output "application" {
  value = {
    appId       = var.app_id
    dbNamespace = local.db_namespace
    database = {
      url         = "https://${module.database.dns_name}:8080"
      tokenSecret = var.db_api_token_secret_arn
      proxy       = { host = module.database.dns_name, port = 6432 }
    }
    redis = {
      url         = "http://${module.redis.dns_name}:8090"
      tokenSecret = var.redis_api_token_secret_arn
      proxy       = { host = module.redis.dns_name, port = 6379 }
    }
  }
}
