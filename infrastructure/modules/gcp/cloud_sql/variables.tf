variable "name" {
  type        = string
  description = "배포별 고유 이름. root에서 검증합니다."
}

variable "labels" {
  type        = map(string)
  description = "필수 추적 메타데이터. 비밀값과 개인정보를 넣지 않습니다."
}

variable "region" {
  type        = string
  description = "GCP region"
}

variable "network_id" {
  type        = string
  description = "private network ID"
}

variable "service_range_name" {
  type        = string
  description = "Private Service Access reserved range name"
}

variable "tier" {
  type        = string
  description = "Cloud SQL machine tier"
}

variable "storage_gib" {
  type        = number
  description = "SSD disk GiB"
}

variable "max_storage_gib" {
  type        = number
  description = "SSD 자동 확장 상한"
}

variable "high_availability" {
  type        = bool
  description = "REGIONAL HA 사용"
}

variable "database_name" {
  type        = string
  description = "초기 논리 DB 이름"
}

variable "app_service_account" {
  type        = string
  description = "IAM DB 사용자로 등록할 앱 SA"
}

variable "deletion_protection" {
  type        = bool
  description = "Terraform/API 삭제 보호. 사람만 변경"
}

variable "admin_user" {
  type    = string
  default = "platformadmin"
}
variable "admin_password" {
  type      = string
  sensitive = true
}
variable "authorized_networks" {
  type        = map(string)
  default     = {}
  description = "공인 IP 접속을 허용할 이름 => CIDR. 비어 있으면 공인 IP를 만들지 않습니다."
}

variable "postgres_version" {
  type    = string
  default = "17"
  validation {
    condition     = contains(["16", "17"], var.postgres_version)
    error_message = "Use PostgreSQL 16 or 17."
  }
}
