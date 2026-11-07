# Application containers run on Fargate (AWS) or Cloud Run (GCP).

variable "app" {
  description = "앱 이미지(ECR)와 실행 방식. env로 DATABASE_URL 등을 덮어쓰면 플랫폼 프록시를 거치게 할 수 있습니다."
  type = object({
    entrypoint        = list(string)
    cpu               = optional(number, 1)
    memory_mib        = optional(number, 2048)
    image             = string
    port              = optional(number, 80)
    health_path       = optional(string, "/readyz")
    api_workers       = optional(number, 4)
    worker_count      = optional(number, 2)
    served_by         = optional(string)
    seed_demo_data    = optional(bool, false)
    seed_close_in_s   = optional(number, 86400)
    env               = optional(map(string), {})
    api_env           = optional(map(string), {})
    worker_env        = optional(map(string), {})
    secret_env        = optional(map(string), {}) # NAME => Secrets Manager ARN
    tunnel_secret     = optional(string)          # Secrets Manager ARN of a Cloudflare tunnel token
    cloudflared_image = optional(string, "cloudflare/cloudflared:2024.12.2")
  })
  validation {
    condition = var.app == null ? true : (
      contains([0.25, 0.5, 1, 2, 4], var.app.cpu) &&
      try(var.app.memory_mib >= { "0.25" = 512, "0.5" = 1024, "1" = 2048, "2" = 4096, "4" = 8192 }[tostring(var.app.cpu)] &&
        var.app.memory_mib <= { "0.25" = 2048, "0.5" = 4096, "1" = 8192, "2" = 16384, "4" = 30720 }[tostring(var.app.cpu)] &&
    var.app.memory_mib % (var.app.cpu == 0.25 ? 512 : 1024) == 0, false))
    error_message = "Use a supported Fargate CPU and memory combination."
  }
  validation {
    condition     = var.app == null ? true : length(var.replication_cidrs) == 0
    error_message = "Managed apps use private subnet routing; use shared Tailscale routes instead of public replication CIDRs."
  }
  default = null
  validation {
    condition     = var.app == null ? true : (var.app.port >= 1 && var.app.port <= 65535 && floor(var.app.port) == var.app.port && can(regex("^/[A-Za-z0-9_./-]*$", var.app.health_path)) && !startswith(var.app.health_path, "//"))
    error_message = "Use an integer port from 1 to 65535 and a local HTTP health path."
  }
  validation {
    condition     = var.app == null ? true : (var.app.worker_count >= 1 && var.app.worker_count <= 8 && var.app.api_workers >= 1 && var.app.api_workers <= 16)
    error_message = "Use 1-8 queue workers and 1-16 API processes."
  }
  validation {
    condition     = var.app == null ? true : can(regex("^[0-9]{12}\\.dkr\\.ecr\\.[a-z0-9-]+\\.amazonaws\\.com/", var.app.image))
    error_message = "app.image는 ECR 이미지 주소입니다 (<계정>.dkr.ecr.<리전>.amazonaws.com/<저장소>:<태그>)."
  }
}
variable "replication_cidrs" {
  type        = set(string)
  default     = []
  nullable    = false
  description = "다른 클라우드의 대상 DB가 RDS로 복제 접속하는 공인 IP. 비어 있지 않으면 RDS에 공인 endpoint가 생깁니다."
}

locals {
  extra_secret_arns = merge(try(var.app.secret_env, {}), try(var.app.tunnel_secret, null) == null ? {} : { TUNNEL = var.app.tunnel_secret })
  app_db_user       = "app"
}

resource "random_password" "app_db" {
  length  = 32
  special = false
}
resource "aws_secretsmanager_secret" "app_db" {
  name_prefix             = "${var.deployment_id}-app-db-"
  description             = "App database role password for ${var.deployment_id}"
  recovery_window_in_days = 0
  tags                    = merge(module.metadata.common, { component = "database" })
}
resource "aws_secretsmanager_secret_version" "app_db" {
  secret_id     = aws_secretsmanager_secret.app_db.id
  secret_string = random_password.app_db.result
}

# What the platform's DB migration adaptor reads for this database (its catalog's secret_ref):
# admin and replication DSNs with TLS verified against the CA the adaptor writes at
# /run/foxtrot/server-ca.crt, and the app role's password that PgBouncer serves the app with.
# Named under foxtrot/ so the adaptor's task role can read it. The admin password comes from the
# RDS-managed secret; if RDS rotates it, re-apply this environment to refresh the DSNs.
data "aws_secretsmanager_secret_version" "rds_master" {
  secret_id = module.rds.master_secret_arn
}
locals {
  rds_master = jsondecode(data.aws_secretsmanager_secret_version.rds_master.secret_string)
  rds_admin_dsn = format("postgresql://%s:%s@%s:%d/%s?sslmode=verify-full&sslrootcert=/run/foxtrot/server-ca.crt",
  urlencode(local.rds_master.username), urlencode(local.rds_master.password), module.rds.address, module.rds.port, var.database_name)
}
resource "aws_secretsmanager_secret" "db_operator" {
  name                    = "foxtrot/app/${var.deployment_id}/db-operator"
  description             = "DB migration adaptor credentials for ${var.deployment_id}"
  recovery_window_in_days = 0
  tags                    = merge(module.metadata.common, { component = "database" })
}
resource "aws_secretsmanager_secret_version" "db_operator" {
  secret_id = aws_secretsmanager_secret.db_operator.id
  secret_string = jsonencode({
    adminDsn       = local.rds_admin_dsn
    replicationDsn = local.rds_admin_dsn
    writerPassword = random_password.app_db.result
  })
}

module "app_runtime" {
  count  = var.app == null ? 0 : 1
  source = "../../modules/app_runtime"
  cloud  = "aws"
  region = var.region
  app    = var.app
  database = {
    host                = module.rds.address
    port                = module.rds.port
    name                = var.database_name
    app_user            = local.app_db_user
    app_password_secret = aws_secretsmanager_secret.app_db.arn
    admin_secret        = module.rds.master_secret_arn
    admin_secret_format = "rds-json"
  }
  redis_url = "${local.redis_transit_encryption ? "rediss" : "redis"}://${module.elasticache.endpoint}:${module.elasticache.port}"
  storage   = { bucket = module.s3.bucket_id, region = var.region }

}

variable "force_destroy_storage" {
  type        = bool
  default     = false
  description = "환경을 지울 때 버킷에 객체가 남아 있어도 지웁니다. 프로비저닝 API가 만든 PoC 환경에서만 켭니다."
}

variable "tailscale" {
  description = "플랫폼과의 사설망: 공유 라우터가 이 키(Secrets Manager ARN)로 가입합니다. router_inputs로 전달하며 앱 VM은 가입하지 않습니다"
  type        = object({ auth_key_secret = string, tag = optional(string, "tag:aws") })
  default     = null
}
