variable "name" {
  type        = string
  description = "배포별 고유 이름. root에서 검증합니다."
}

variable "tags" {
  type        = map(string)
  description = "필수 추적 메타데이터. 비밀값과 개인정보를 넣지 않습니다."
}

variable "subnet_ids" {
  type        = list(string)
  description = "private subnet IDs"
}

variable "security_group_id" {
  type        = string
  description = "cache SG ID"
}

variable "node_type" {
  type        = string
  description = "Redis node 사양"
}

variable "node_count" {
  type        = number
  description = "1~3개 node; 2개 이상이면 자동 failover/Multi-AZ"
}
