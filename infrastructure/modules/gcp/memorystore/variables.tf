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
  description = "PSA reserved range name"
}

variable "memory_gib" {
  type        = number
  description = "Redis memory GiB"
}

variable "high_availability" {
  type        = bool
  description = "STANDARD_HA 사용"
}

variable "auth_enabled" {
  type    = bool
  default = false
}
variable "transit_encryption" {
  type    = bool
  default = false
}
