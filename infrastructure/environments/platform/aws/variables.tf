variable "region" {
  type    = string
  default = "ap-northeast-2"
}
variable "name" {
  type    = string
  default = "foxtrot-platform"
}
variable "vpc_cidr" {
  type        = string
  default     = "10.30.0.0/16"
  description = "관리 VPC. 팀 대역 계획(traffic): AWS 앱 10.10, GCP 앱 10.20/10.21, 관리 10.30, Tailscale 100.64/10"
}
variable "azs" {
  type    = list(string)
  default = ["ap-northeast-2a", "ap-northeast-2c"]
}
variable "app_cidrs" {
  type        = list(string)
  default     = ["10.10.0.0/16", "10.20.0.0/16", "10.21.0.0/16"]
  description = "앱 환경 대역. 관리 VPC에서 이 대역으로 가는 트래픽은 게이트웨이(Tailscale)로 보냅니다."
}
variable "gateway_instance_type" {
  type    = string
  default = "t3.small"
}
variable "image_tags" {
  type        = map(string)
  default     = {}
  description = "서비스 이름 => 이미지 태그. 없으면 latest"
}
variable "gcp_project_id" {
  type        = string
  description = "플랫폼 서비스가 접근하는 GCP 프로젝트 (Cloud SQL 조회, GCS, 앱 환경 생성)"
}
variable "console_url" {
  type        = string
  default     = "https://platform.foxtrot.live"
  description = "콘솔 주소 (DNS는 Cloudflare). GitHub 로그인 콜백(<주소>/api/auth/github/callback)과 Origin 검사에 씁니다."
}
variable "github_client_id" {
  type        = string
  default     = "Ov23liYMrR7123mf374C"
  description = "org 소유 GitHub 앱의 Client ID (비밀 아님). secret은 Secrets Manager foxtrot/platform/github-client-secret"
}
variable "github_allowed_ids" {
  type = list(number)
  # org 멤버 전원: dlacowns21, hyunsuhahaha, noeulnight, SuperSon7, yyoungjin
  default     = [176018876, 76990163, 44047052, 121543941, 133024268]
  description = "콘솔 로그인을 허용할 GitHub 숫자 ID"
}
variable "redis_enabled" {
  type        = bool
  default     = true
  description = "Redis 프록시·statemig·sync worker 실행. 프록시는 시작할 때 원본·대상 Redis에 닿아야 하므로 앱 환경과 redis-endpoints 비밀이 준비된 뒤 켭니다."
}
variable "object_resources" {
  type        = string
  default     = "{}"
  description = "object-migration OBJECT_RESOURCES (이전 대상 버킷 목록, JSON)"
}
variable "traffic" {
  description = "트래픽 서비스의 Cloudflare 식별자 (트래픽 파트 값). 비어 있으면 /v1이 503"
  type = object({
    zone_id    = optional(string, "")
    account_id = optional(string, "")
    lb_id      = optional(string, "")
    pool_ids   = optional(string, "")
  })
  # app.foxtrot.live load balancer and its two pools (IDs are not secret).
  default = {
    zone_id    = "a347c7267257bd2d1b3fe35d0d62ac72"
    account_id = "3d6694c353d2e0519e3215805b763743"
    lb_id      = "6bf4a03e3d63cb3da4a19e6d0c69a606"
    pool_ids   = "{\"aws\":\"b26f0a79ed2d63568022f5b0ffd2b00d\",\"gcp\":\"c02ba1a4acbbbb702fd2f67d6b6dc6a4\"}"
  }
}

variable "redis_endpoints" {
  description = "Redis 프록시 백엔드: 이름 => redis:// 주소 (첫 항목이 처음 활성). 사설망(Tailscale)으로 닿는 앱 환경의 Redis"
  type        = map(string)
  # chiikawa: AWS ElastiCache (source) and GCP Memorystore (target), both reached over the tailnet.
  default = {
    aws = "redis://chiikawa-aws-3e3d-cache.nea7q9.ng.0001.apn2.cache.amazonaws.com:6379"
    gcp = "redis://10.21.107.99:6379"
  }
}

variable "observability" {
  description = "Private OTEL stack from observability/compose.yaml. Headers secret contains Authorization=Bearer%20<module-token>. Null disables SDK export."
  type = object({
    endpoint           = string
    headers_secret_arn = string
    prometheus_url     = string
    tempo_url          = string
  })
  default = null
}
