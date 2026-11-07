variable "project_id" {
  type        = string
  description = "사람이 선택한 기존 GCP 프로젝트. billing 연결과 API enable 권한이 필요합니다."
}
variable "zone" {
  type        = string
  description = "region 내부의 zone"
  validation {
    condition     = startswith(var.zone, "${var.region}-")
    error_message = "zone은 선택한 region에 속해야 합니다."
  }
}
variable "network" {
  type        = object({ subnet_cidr = string, service_range_address = string, service_range_prefix = number })
  description = "Private Service Access 범위는 앱 subnet과 겹치면 안 됩니다. validate 명령으로 확인하세요."
  validation {
    condition     = can(cidrnetmask(var.network.subnet_cidr)) && can(cidrnetmask("${var.network.service_range_address}/${var.network.service_range_prefix}")) && var.network.service_range_prefix >= 16 && var.network.service_range_prefix <= 20
    error_message = "유효한 IPv4 subnet과 /16~20 service range를 지정하세요."
  }
}
variable "image" {
  type        = string
  default     = "debian-cloud/debian-12"
  description = "검토한 image 또는 family. 재현 가능한 배포에서는 구체적인 image를 고정하세요."
}
provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}
resource "google_project_service" "required" {
  for_each           = toset(["compute.googleapis.com", "iam.googleapis.com", "sqladmin.googleapis.com", "redis.googleapis.com", "servicenetworking.googleapis.com", "storage.googleapis.com", "logging.googleapis.com", "monitoring.googleapis.com", "oslogin.googleapis.com", "artifactregistry.googleapis.com", "secretmanager.googleapis.com", "cloudresourcemanager.googleapis.com", "run.googleapis.com"])
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

variable "proxy_cidrs" {
  type        = set(string)
  default     = []
  nullable    = false
  description = "기존 중앙 Redis 프록시/PgBouncer의 사설 목적지. 라우팅은 사전 구성."
  validation {
    condition = alltrue([for c in var.proxy_cidrs : try(
      can(cidrnetmask(c)) && tonumber(split("/", c)[1]) >= 24 &&
    can(regex("^(10\\.|192\\.168\\.|172\\.(1[6-9]|2[0-9]|3[01])\\.|100\\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\\.)", cidrhost(c, 0))), false)])
    error_message = "Use explicit RFC1918 or Tailscale IPv4 CIDRs, /24 through /32."
  }
}
variable "enable_cloudflare_tunnel" {
  type        = bool
  default     = false
  nullable    = false
  description = "cloudflared용 outbound TCP/UDP 7844. 터널 설치/등록은 별도 배포."
}

variable "postgres_version" {
  type    = string
  default = null
  validation {
    condition     = var.postgres_version == null ? true : contains(["16", "17"], var.postgres_version)
    error_message = "Use PostgreSQL 16 or 17."
  }
}
locals {
  postgres_version         = coalesce(var.postgres_version, var.app == null ? "16" : "17")
  redis_auth               = coalesce(var.redis_auth, var.app == null)
  redis_transit_encryption = coalesce(var.redis_transit_encryption, var.app == null)
}

locals { managed_app = var.app != null }
