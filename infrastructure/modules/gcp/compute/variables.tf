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

variable "zone" {
  type        = string
  description = "GCP zone"
}

variable "machine_type" {
  type        = string
  description = "VM machine type"
}

variable "boot_disk_gib" {
  type        = number
  description = "boot disk GiB"
}

variable "image" {
  type        = string
  description = "검토한 OS image 또는 image family"
}

variable "subnetwork_id" {
  type        = string
  description = "앱 subnet ID"
}

variable "service_account_email" {
  type        = string
  description = "전용 앱 service account email"
}

variable "startup_script" {
  type        = string
  default     = null
  description = "부팅마다 실행하는 스크립트 (멱등)"
}

variable "can_ip_forward" {
  type        = bool
  default     = false
  description = "true: 남의 주소로 가는 패킷을 전달 (Tailscale 서브넷 라우터)"
}
