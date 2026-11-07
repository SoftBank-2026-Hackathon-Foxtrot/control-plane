variable "name" {
  type        = string
  description = "배포별 고유 이름. root에서 검증합니다."
}

variable "tags" {
  type        = map(string)
  description = "필수 추적 메타데이터. 비밀값과 개인정보를 넣지 않습니다."
}

variable "vpc_cidr" {
  type        = string
  description = "VPC IPv4 CIDR"
}

variable "public_subnet_cidr" {
  type        = string
  description = "PoC 앱용 public subnet CIDR"
}

variable "private_subnet_cidrs" {
  type        = list(string)
  description = "DB/cache용 서로 다른 두 AZ의 private subnet CIDR"
}

variable "availability_zones" {
  type        = list(string)
  description = "최소 두 개의 가용 영역"
}

variable "data_subnets_internet_route" {
  type        = bool
  default     = false
  description = "데이터 subnet에 IGW 경로를 둡니다. 다른 클라우드에서 DB로 복제 접속할 때만 사용합니다."
}
