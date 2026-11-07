variable "name" {
  type    = string
  default = "foxtrot-aws-tailscale"
}
variable "region" {
  type    = string
  default = "ap-northeast-2"
}
variable "account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Use the expected 12-digit AWS account ID."
  }
}
variable "vpc_id" { type = string }
variable "subnet_id" {
  type        = string
  description = "Existing public subnet with an internet gateway route for Tailscale control/peer traffic."
}
variable "route_table_ids" {
  type        = set(string)
  description = "Existing worker and DB subnet route tables; includes the DB return path."
  validation {
    condition     = length(var.route_table_ids) > 0
    error_message = "At least one existing client route table is required."
  }
}
variable "remote_cidrs" { type = set(string) }
variable "database_security_group_ids" {
  type        = set(string)
  default     = []
  description = "Optional existing DB security groups that need PostgreSQL ingress from remote private CIDRs."
}
variable "client_security_group_ids" {
  type        = set(string)
  default     = []
  description = "Existing Worker/PgBouncer/Redis proxy/app groups requiring selected database ports to remote CIDRs."
}
variable "redis_ports" {
  type        = set(number)
  default     = []
  description = "Opt-in Redis backend TCP ports. Use 6379 for ElastiCache/plain Redis and 6378 for Memorystore Redis TLS."
  validation {
    condition     = alltrue([for port in var.redis_ports : contains([6378, 6379], port)])
    error_message = "Only supported Redis backend ports 6378 and 6379 may be enabled."
  }
}
variable "cache_security_group_ids" {
  type        = set(string)
  default     = []
  description = "Existing Redis security groups; only redis_ports are opened from remote CIDRs."
  validation {
    condition     = length(var.cache_security_group_ids) == 0 || length(var.redis_ports) > 0
    error_message = "Select redis_ports when opening a cache security group."
  }
}
variable "ami_id" {
  type        = string
  description = "Reviewed Ubuntu 24.04 LTS amd64 AMI in this region. No credentials in user data."
}
variable "instance_type" {
  type    = string
  default = "t3.small"
  validation {
    condition     = can(regex("^t3[a]?\\.", var.instance_type))
    error_message = "Use an x86 T3/T3a type with standard CPU credits for this small router."
  }
}
variable "enable_ssm" {
  type        = bool
  default     = false
  description = "Enable Systems Manager access for router verification without opening SSH."
}
variable "auth_key_secret_arn" {
  type        = string
  description = "Existing Secrets Manager plain-string reusable, preauthorized, non-ephemeral Tailscale auth key."
  validation {
    condition     = can(regex("^arn:aws:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:.+$", var.auth_key_secret_arn))
    error_message = "Provide a Secrets Manager secret ARN, never an auth key value."
  }
}
variable "secret_kms_key_arn" {
  type    = string
  default = null
}
variable "tailscale_version" {
  type    = string
  default = "1.104.4"
}
variable "tags" {
  type    = map(string)
  default = {}
}

variable "app_ports" {
  type        = set(number)
  default     = []
  description = "App HTTP readiness ports, restricted to remote_cidrs."
  validation {
    condition     = alltrue([for p in var.app_ports : p >= 1 && p <= 65535 && floor(p) == p])
    error_message = "Use integer TCP ports from 1 to 65535."
  }
}
variable "service_ports" {
  type        = set(number)
  default     = []
  description = "Explicit platform proxy/adapter ports required over the routed private networks."
  validation {
    condition     = alltrue([for p in var.service_ports : contains([6432, 7000, 8000, 8001, 8080, 8090], p)])
    error_message = "Select only the platform's known proxy/adapter ports."
  }
}

variable "app_security_group_ids" {
  type    = set(string)
  default = []
}
