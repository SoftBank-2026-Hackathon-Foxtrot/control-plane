variable "name" {
  type        = string
  description = "배포별 고유 이름. root에서 검증합니다."
}

variable "tags" {
  type        = map(string)
  description = "필수 추적 메타데이터. 비밀값과 개인정보를 넣지 않습니다."
}

variable "ami_id" {
  type        = string
  description = "검토한 x86_64 AMI ID"
}

variable "instance_type" {
  type        = string
  description = "인스턴스 사양"
}

variable "root_disk_gib" {
  type        = number
  description = "암호화 gp3 루트 디스크 GiB"
}

variable "subnet_id" {
  type        = string
  description = "public subnet ID"
}

variable "security_group_id" {
  type        = string
  description = "앱 SG ID"
}

variable "instance_profile_name" {
  type        = string
  description = "앱 IAM instance profile"
}

variable "key_name" {
  type        = string
  description = "SSH를 사용할 때 기존 EC2 key pair 이름"
  default     = null
}

variable "user_data" {
  type        = string
  default     = null
  description = "첫 부팅 때 실행할 스크립트. 바뀌면 인스턴스를 교체합니다."
}

variable "source_dest_check" {
  type        = bool
  default     = true
  description = "false: 남의 주소로 가는 패킷을 전달 (Tailscale 서브넷 라우터)"
}
