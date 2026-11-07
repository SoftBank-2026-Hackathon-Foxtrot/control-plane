variable "name" {
  type        = string
  description = "배포별 고유 이름. root에서 검증합니다."
}

variable "tags" {
  type        = map(string)
  description = "필수 추적 메타데이터. 비밀값과 개인정보를 넣지 않습니다."
}

variable "vpc_id" {
  type        = string
  description = "VPC ID"
}

variable "web_ingress_cidrs" {
  type        = set(string)
  description = "앱 웹 포트에 접근할 IPv4 CIDR"
}

variable "web_ports" {
  type        = set(number)
  description = "공개할 앱 TCP 포트"
}

variable "admin_cidrs" {
  type        = set(string)
  description = "선택적 SSH 관리자 CIDR. 기본 비어 있음"
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
variable "migration_source_cidrs" {
  type        = set(string)
  default     = []
  nullable    = false
  description = "관리 프록시/DB 이전기의 실제 접속 원본 IP. SNAT 사용 시 라우터 사설 IP."
  validation {
    condition = alltrue([for c in var.migration_source_cidrs : try(
      can(cidrnetmask(c)) && tonumber(split("/", c)[1]) >= 24 &&
    can(regex("^(10\\.|192\\.168\\.|172\\.(1[6-9]|2[0-9]|3[01])\\.|100\\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\\.)", cidrhost(c, 0))), false)])
    error_message = "Use explicit RFC1918 or Tailscale IPv4 CIDRs, /24 through /32."
  }
}

variable "replication_cidrs" {
  type        = set(string)
  default     = []
  nullable    = false
  description = "다른 클라우드의 대상 DB가 이 DB로 복제 접속하는 공인 IP (/32 권장)"
  validation {
    condition     = alltrue([for c in var.replication_cidrs : can(cidrnetmask(c)) && tonumber(split("/", c)[1]) >= 24])
    error_message = "복제 접속 허용은 /24~/32 CIDR만 받습니다."
  }
}

variable "enable_tailscale" {
  type    = bool
  default = false
}
