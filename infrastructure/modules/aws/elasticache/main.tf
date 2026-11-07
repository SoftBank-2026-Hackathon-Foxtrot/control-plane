variable "transit_encryption" {
  type        = bool
  default     = false
  description = "TLS와 AUTH. 팀 Redis 프록시(redis-migrator)가 TLS 백엔드를 지원하지 않으므로 기본은 끕니다. 접근은 security group으로 제한합니다."
}
variable "auth_token" {
  type        = string
  default     = null
  sensitive   = true
  description = "transit_encryption=true일 때만 사용. 외부 secret injection으로 제공. Terraform state에 저장되므로 state 접근을 제한합니다."
  validation {
    condition     = var.auth_token == null || can(regex("^[a-zA-Z0-9!&#$^<>-]{16,128}$", var.auth_token))
    error_message = "Redis AUTH는 16~128자의 허용된 문자만 사용합니다."
  }
}
resource "aws_elasticache_subnet_group" "this" {
  name       = var.name
  subnet_ids = var.subnet_ids
  tags       = var.tags
}
resource "aws_elasticache_parameter_group" "migration" {
  name   = "${var.name}-migration"
  family = "redis7"
  parameter {
    name  = "notify-keyspace-events"
    value = "KEA"
  }
  parameter {
    name  = "maxmemory-policy"
    value = "noeviction"
  }
  tags = var.tags
}
resource "aws_elasticache_replication_group" "this" {
  replication_group_id       = var.name
  description                = "${var.name} application cache"
  engine                     = "redis"
  engine_version             = "7.1"
  parameter_group_name       = aws_elasticache_parameter_group.migration.name
  node_type                  = var.node_type
  num_cache_clusters         = var.node_count
  automatic_failover_enabled = var.node_count > 1
  multi_az_enabled           = var.node_count > 1
  subnet_group_name          = aws_elasticache_subnet_group.this.name
  security_group_ids         = [var.security_group_id]
  port                       = 6379
  at_rest_encryption_enabled = true
  transit_encryption_enabled = var.transit_encryption
  transit_encryption_mode    = var.transit_encryption ? "required" : null
  auth_token                 = var.transit_encryption ? var.auth_token : null
  auth_token_update_strategy = var.transit_encryption ? "SET" : null
  snapshot_retention_limit   = 1
  apply_immediately          = false
  tags                       = var.tags
  lifecycle {
    precondition {
      condition     = !var.transit_encryption || var.auth_token != null
      error_message = "transit_encryption=true이면 auth_token이 필요합니다."
    }
  }
}
