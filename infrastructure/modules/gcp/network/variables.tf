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

variable "subnet_cidr" {
  type        = string
  description = "앱 subnet CIDR"
}

variable "service_range_address" {
  type        = string
  description = "Private Service Access 예약 범위 시작 IP"
}

variable "service_range_prefix" {
  type        = number
  description = "Private Service Access 예약 prefix length"
}
