variable "cloud" {
  type        = string
  description = "aws 또는 gcp. 비밀값 조회와 레지스트리 로그인 방식을 정합니다."
  validation {
    condition     = contains(["aws", "gcp"], var.cloud)
    error_message = "cloud는 aws 또는 gcp입니다."
  }
}
variable "region" {
  type        = string
  description = "AWS 리전(Secrets Manager·ECR 조회). GCP는 참고용"
}
variable "app" {
  description = "레지스트리에 이미 있는 앱 이미지와 실행 방식"
  type = object({
    image             = string
    port              = optional(number, 80)
    api_workers       = optional(number, 4)
    worker_count      = optional(number, 2)
    served_by         = optional(string)
    seed_demo_data    = optional(bool, false)
    seed_close_in_s   = optional(number, 86400)
    env               = optional(map(string), {})
    api_env           = optional(map(string), {})
    worker_env        = optional(map(string), {})
    secret_env        = optional(map(string), {})
    tunnel_secret     = optional(string)
    cloudflared_image = optional(string, "cloudflare/cloudflared:2024.12.2")
  })
  validation {
    condition     = var.app.worker_count >= 1 && var.app.worker_count <= 8 && var.app.api_workers >= 1 && var.app.api_workers <= 16
    error_message = "worker_count는 1~8, api_workers는 1~16입니다."
  }
  validation {
    condition     = alltrue([for k, v in merge(var.app.env, var.app.api_env, var.app.worker_env) : can(regex("^[A-Z_][A-Z0-9_]*$", k)) && !strcontains(v, "\n") && !strcontains(v, "'")])
    error_message = "환경 변수 이름은 대문자/숫자/_이고, 값에 줄바꿈이나 작은따옴표가 없어야 합니다."
  }
  validation {
    condition     = alltrue([for k in keys(var.app.secret_env) : can(regex("^[A-Z_][A-Z0-9_]*$", k))])
    error_message = "secret_env 이름은 대문자/숫자/_입니다."
  }
}
variable "database" {
  type = object({
    host                = string
    port                = number
    name                = string
    app_user            = string
    app_password_secret = string
    admin_secret        = string
    admin_secret_format = string # rds-json: RDS managed master secret / plain: password only
    admin_user          = optional(string)
  })
  validation {
    condition     = contains(["rds-json", "plain"], var.database.admin_secret_format) && (var.database.admin_secret_format == "rds-json" || var.database.admin_user != null)
    error_message = "admin_secret_format은 rds-json 또는 plain이고, plain이면 admin_user가 필요합니다."
  }
}
variable "redis_url" {
  type        = string
  description = "비밀값 없는 Redis 주소"
}
variable "storage" {
  type = object({
    bucket        = string
    region        = string
    endpoint      = optional(string)
    access_key_id = optional(string)
    secret        = optional(string) # 비밀 저장소에 있는 access key secret 참조
  })
}

variable "tailscale" {
  description = "앱 VM을 tailnet에 가입시키고 자기 대역을 광고합니다 (플랫폼과의 사설망, *.internal DNS). null이면 가입하지 않음"
  type = object({
    auth_key_secret = string       # 비밀 저장소에 있는 인증 키 참조
    tag             = string       # tag:aws / tag:gcp
    routes          = list(string) # 광고할 VPC 대역
    hostname        = string
  })
  default = null
}
