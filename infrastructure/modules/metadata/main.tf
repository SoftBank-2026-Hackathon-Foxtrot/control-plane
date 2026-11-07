variable "deployment_id" {
  type        = string
  description = "전역에서 구분 가능한 배포 ID. 두 클라우드의 배포에는 서로 다른 ID를 사용합니다."
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,19}$", var.deployment_id)) && !endswith(var.deployment_id, "-")
    error_message = "deployment_id는 소문자로 시작하는 3~20자 영문 소문자/숫자/하이픈입니다. 끝은 하이픈일 수 없습니다."
  }
}
variable "metadata" {
  type = object({
    project      = string
    environment  = string
    owner        = string
    cost_center  = string
    workload     = string
    migration_id = string
    data_class   = string
    expires_on   = string
  })
  description = "공통 비용/소유권/분류 메타데이터. 날짜는 YYYY-MM-DD이며 자동 삭제를 뜻하지 않습니다."
  validation {
    condition     = alltrue([for v in values(var.metadata) : can(regex("^[a-z0-9][a-z0-9_-]{0,62}$", v))])
    error_message = "메타데이터 값은 GCP label 호환 1~63자 소문자/숫자/_/- 형식입니다."
  }
  validation {
    condition     = contains(["poc", "dev", "staging", "prod"], var.metadata.environment) && contains(["public", "internal", "confidential"], var.metadata.data_class)
    error_message = "environment 또는 data_class 분류를 확인하세요."
  }
  validation {
    condition     = can(formatdate("YYYY-MM-DD", "${var.metadata.expires_on}T00:00:00Z"))
    error_message = "expires_on은 유효한 YYYY-MM-DD 날짜입니다."
  }
}
locals {
  common = merge(var.metadata, {
    deployment_id    = var.deployment_id
    managed_by       = "terraform"
    template_version = "v0-1"
    security_profile = "public-app-private-data-v1"
  })
}
output "common" { value = local.common }
