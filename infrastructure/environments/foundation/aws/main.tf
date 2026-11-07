# Pre-resources for user apps on AWS, made once per account: the image registry, the
# provisioner's Terraform state bucket and the secret slots app VMs read at boot. Everything
# else is created per environment by environments/aws.
terraform {
  required_version = ">= 1.10.0, < 2.0.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

variable "region" {
  type    = string
  default = "ap-northeast-2"
}
variable "repositories" {
  type    = set(string)
  default = ["chiikawa-groupbuy"]
}
variable "secret_slots" {
  type        = map(string)
  default     = { "foxtrot/tailscale-authkey-aws" = "Tailscale auth key (tag:aws) for app VMs" }
  description = "값 없이 만들어 두는 비밀 자리. 값은 소유 파트가 넣습니다."
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { project = "foxtrot", managed_by = "terraform", component = "foundation" }
  }
}

data "aws_caller_identity" "current" {}

resource "aws_ecr_repository" "app" {
  for_each             = var.repositories
  name                 = each.value
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "AES256" }
}
resource "aws_ecr_lifecycle_policy" "app" {
  for_each   = aws_ecr_repository.app
  repository = each.value.name
  policy = jsonencode({ rules = [{
    rulePriority = 1, description = "keep the 10 most recent images"
    selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
    action       = { type = "expire" }
  }] })
}

# Terraform state for environments the provisioner creates (provisioner config: stateBackend).
resource "aws_s3_bucket" "state" {
  bucket = "foxtrot-provisioner-state-${data.aws_caller_identity.current.account_id}"
}
resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_secretsmanager_secret" "slot" {
  for_each    = var.secret_slots
  name        = each.key
  description = each.value
}

output "image_prefix" {
  value = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.region}.amazonaws.com/"
}
output "state_bucket" {
  value = aws_s3_bucket.state.id
}
output "secret_slots" {
  value = { for k, s in aws_secretsmanager_secret.slot : k => s.arn }
}
