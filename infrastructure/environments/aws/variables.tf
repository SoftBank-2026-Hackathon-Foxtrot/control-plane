variable "deployment_id" {
  type        = string
  description = "배포별 고유 식별자. 각 클라우드에 서로 다른 값을 사용합니다."
}
variable "metadata" {
  type        = object({ project = string, environment = string, owner = string, cost_center = string, workload = string, migration_id = string, data_class = string, expires_on = string })
  description = "비용·소유권·분류 메타데이터. metadata 모듈에서 형식을 검증합니다."
  validation {
    condition     = contains(["poc", "dev"], var.metadata.environment)
    error_message = "이 public VM 초안은 poc/dev 전용입니다. production은 별도 검토한 구성으로 만드세요."
  }
}
variable "region" {
  type        = string
  description = "사람이 선택하는 리전. AI 추천 범위 밖입니다."
}
variable "bucket_name" {
  type        = string
  description = "클라우드 내에서 전역 고유한 bucket 이름. 개인정보를 넣지 마세요."
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,61}[a-z0-9]$", var.bucket_name))
    error_message = "bucket_name은 소문자로 시작하는 3~63자 소문자/숫자/하이픈입니다."
  }
}
variable "database_name" {
  type        = string
  default     = "app"
  description = "초기 PostgreSQL DB 이름. 테이블/스키마 GRANT는 후속 모듈에서 처리합니다."
  validation {
    condition     = can(regex("^[a-z][a-z0-9]{0,30}$", var.database_name))
    error_message = "DB 이름은 소문자로 시작하는 최대 31자 영문/숫자입니다."
  }
}
variable "web_ingress_cidrs" {
  type        = set(string)
  default     = ["0.0.0.0/0"]
  description = "PoC 공개 웹 접근 범위. 사람만 변경합니다."
  validation {
    condition     = length(var.web_ingress_cidrs) > 0 && alltrue([for c in var.web_ingress_cidrs : can(cidrnetmask(c))])
    error_message = "웹 접근 범위에는 유효한 IPv4 CIDR이 필요합니다."
  }
}
variable "web_ports" {
  type        = set(number)
  default     = [80, 443]
  description = "공개 앱 포트. 인증서/웹서버 설치는 앱 배포 담당입니다."
  validation {
    condition     = length(var.web_ports) > 0 && alltrue([for p in var.web_ports : contains([80, 443, 8080], p)])
    error_message = "초안의 공개 포트는 80/443/8080으로 제한합니다."
  }
}
variable "admin_cidrs" {
  type        = set(string)
  default     = []
  description = "선택적 SSH 접근 범위. 기본 SSH ingress 없음. /24 이상 좁은 IPv4만 허용합니다."
  validation {
    condition     = alltrue([for c in var.admin_cidrs : can(cidrnetmask(c)) && try(tonumber(split("/", c)[1]) >= 24, false)])
    error_message = "SSH는 유효한 /24~32 IPv4 관리자 CIDR만 허용합니다."
  }
}
variable "deletion_protection" {
  type        = bool
  default     = true
  description = "DB 삭제 보호. 정리 절차를 검토한 사람이 명시적으로 변경합니다. AI 입력에서 제외합니다."
}
variable "sizing" {
  type = object({
    compute_instance_type = optional(string, "t3.small")
    compute_disk_gib      = optional(number, 30)
    db_instance_class     = optional(string, "db.t3.micro")
    db_storage_gib        = optional(number, 20)
    db_max_storage_gib    = optional(number, 100)
    db_multi_az           = optional(bool, false)
    cache_node_type       = optional(string, "cache.t3.micro")
    cache_node_count      = optional(number, 1)
  })
  default     = {}
  description = "AI 추천 가능 항목. 카탈로그·형식·상한을 검증하고 사람이 plan을 검토합니다."
  validation {
    condition = (contains(["t3.small", "t3.medium", "m6i.large"], var.sizing.compute_instance_type) &&
      (var.sizing.compute_disk_gib >= 20 && var.sizing.compute_disk_gib <= 200 && floor(var.sizing.compute_disk_gib) == var.sizing.compute_disk_gib) &&
      contains(["db.t3.micro", "db.t3.small", "db.m6g.large"], var.sizing.db_instance_class) &&
      (var.sizing.db_storage_gib >= 20 && var.sizing.db_storage_gib <= 200 && floor(var.sizing.db_storage_gib) == var.sizing.db_storage_gib) &&
      (var.sizing.db_max_storage_gib >= 22 && var.sizing.db_max_storage_gib <= 500 && floor(var.sizing.db_max_storage_gib) == var.sizing.db_max_storage_gib) &&
      contains(["cache.t3.micro", "cache.t3.small", "cache.m6g.large"], var.sizing.cache_node_type) &&
      (var.sizing.cache_node_count >= 1 && var.sizing.cache_node_count <= 3 && floor(var.sizing.cache_node_count) == var.sizing.cache_node_count) &&
    var.sizing.db_max_storage_gib >= ceil(var.sizing.db_storage_gib * 1.1))
    error_message = "sizing은 catalog/sizing-policy.json의 허용 후보·정수 범위와 DB 확장 상한(초기 크기의 110% 이상)을 만족해야 합니다."
  }
}
