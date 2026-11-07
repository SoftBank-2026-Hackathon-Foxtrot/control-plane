variable "name" {
  type        = string
  description = "배포별 고유 이름. root에서 검증합니다."
}

variable "tags" {
  type        = map(string)
  description = "필수 추적 메타데이터. 비밀값과 개인정보를 넣지 않습니다."
}

variable "bucket_arn" {
  type        = string
  description = "앱이 접근할 단일 bucket ARN"
}

variable "secret_arns" {
  type        = list(string)
  default     = []
  description = "앱 VM이 부팅할 때 읽는 Secrets Manager ARN"
}
