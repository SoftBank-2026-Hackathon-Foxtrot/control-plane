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
  description = "DB SG ID"
}

variable "instance_class" {
  type        = string
  description = "RDS 인스턴스 사양"
}

variable "storage_gib" {
  type        = number
  description = "최초 gp3 저장 용량"
}

variable "max_storage_gib" {
  type        = number
  description = "스토리지 자동 확장 상한"
}

variable "multi_az" {
  type        = bool
  description = "Multi-AZ 사용"
}

variable "engine_version" {
  type        = string
  description = "검증한 PostgreSQL 16 minor 버전"
}

variable "database_name" {
  type        = string
  description = "초기 논리 DB 이름"
}

variable "deletion_protection" {
  type        = bool
  description = "삭제 방지. 사람만 변경"
}

variable "final_snapshot_identifier" {
  type        = string
  description = "삭제 시 사용할 고유 final snapshot 이름"
}

variable "publicly_accessible" {
  type        = bool
  default     = false
  description = "Give the instance a public endpoint. Only for cross-cloud replication; the security group still limits who can connect."
}
