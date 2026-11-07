variable "name" {
  type    = string
  default = "foxtrot-gcp-tailscale"
  validation {
    condition     = length(var.name) <= 30
    error_message = "The GCP service account ID must be at most 30 characters."
  }
}
variable "project_id" { type = string }
variable "region" {
  type    = string
  default = "asia-northeast3"
}
variable "zone" {
  type    = string
  default = "asia-northeast3-a"
  validation {
    condition     = startswith(var.zone, "${var.region}-")
    error_message = "The router zone must belong to the selected region."
  }
}
variable "network_name" { type = string }
variable "subnetwork_name" { type = string }
variable "service_cidrs" {
  type        = set(string)
  default     = []
  description = "Existing Cloud SQL/Memorystore Private Service Access ranges. Memorystore producer-side custom route import must be enabled separately."
  validation {
    condition     = length(var.service_cidrs) == 0 || var.private_services_peering_name != null
    error_message = "Private Service Access CIDRs require the existing peering name for return routes."
  }
}
variable "private_services_peering_name" {
  type        = string
  default     = null
  description = "Existing PSA peering whose consumer-side custom route export this state manages; do not manage it twice."
}
variable "private_services_import_custom_routes" {
  type        = bool
  default     = null
  description = "Required when managing PSA: copy its existing import_custom_routes value to preserve it."
}
variable "remote_cidrs" { type = set(string) }
variable "database_network_tags" {
  type        = set(string)
  default     = []
  description = "Optional tags of self-managed DB VMs requiring remote PostgreSQL ingress. Does not apply to Cloud SQL."
}
variable "client_service_account_emails" {
  type        = set(string)
  default     = []
  description = "Existing app/Worker VM identities needing remote DB egress before the app default deny."
}
variable "client_network_tags" {
  type        = set(string)
  default     = []
  description = "Cloud Run Direct VPC network tags requiring remote database and proxy egress."
}
variable "redis_ports" {
  type        = set(number)
  default     = []
  description = "Opt-in Redis backend TCP ports: 6379 plain Redis/ElastiCache, 6378 Memorystore Redis TLS."
  validation {
    condition     = alltrue([for port in var.redis_ports : contains([6378, 6379], port)])
    error_message = "Only supported Redis backend ports 6378 and 6379 may be enabled."
  }
}
variable "cache_network_tags" {
  type        = set(string)
  default     = []
  description = "Optional self-managed Redis VM tags requiring remote ingress. Managed Memorystore uses PSA, not these tags."
  validation {
    condition     = length(var.cache_network_tags) == 0 || length(var.redis_ports) > 0
    error_message = "Select redis_ports when opening a cache VM firewall."
  }
}
variable "auth_key_secret_id" {
  type        = string
  description = "Existing Secret Manager secret ID containing a reusable, preauthorized, non-ephemeral auth key."
  validation {
    condition     = can(regex("^[a-zA-Z0-9_-]+$", var.auth_key_secret_id))
    error_message = "Provide a secret ID, never an auth key value or full secret version payload."
  }
}
variable "machine_type" {
  type    = string
  default = "e2-small"
}
variable "image" {
  type    = string
  default = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
}
variable "tailscale_version" {
  type    = string
  default = "1.104.4"
}
variable "labels" {
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
