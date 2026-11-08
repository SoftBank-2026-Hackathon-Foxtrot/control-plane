terraform {
  required_version = ">= 1.11.0, < 2.0.0"
  required_providers {
    aws       = { source = "hashicorp/aws", version = "~> 6.0" }
    google    = { source = "hashicorp/google", version = "~> 7.0" }
    tailscale = { source = "tailscale/tailscale", version = "~> 0.21" }
  }
}
variable "account_id" { type = string }
variable "region" { type = string }
variable "project_id" { type = string }
variable "prefix" { type = string }
variable "api_key" {
  type      = string
  sensitive = true
  ephemeral = true
}
variable "routes" { type = map(list(string)) }
provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
  default_tags { tags = { project = "foxtrot", environment = "dev", deployment_id = var.prefix, managed_by = "terraform" } }
}
provider "google" { project = var.project_id }
provider "tailscale" { tailnet = var.tailscale_tailnet }

variable "tailscale_tailnet" {
  description = "Tailnet managed by this root. Only the Foxtrot team tailnet gets its demo VM rules."
  type        = string
  default     = "softbank-2026-hackathon-foxtrot.org.github"
  validation {
    condition     = length(trimspace(var.tailscale_tailnet)) > 0 && var.tailscale_tailnet != "-"
    error_message = "Name the tailnet explicitly so an implicit credential cannot overwrite the wrong policy."
  }
}

locals {
  onprem_policy = jsondecode(var.tailscale_tailnet == "softbank-2026-hackathon-foxtrot.org.github" ? file("${path.module}/../../../catalog/foxtrot-onprem-acl.json") : "{\"tagOwners\":{},\"acls\":[]}")
  router_roles  = toset(["aws", "gcp", "mgmt"])
  router_tags   = [for role in sort(tolist(local.router_roles)) : "tag:${role}"]
}

# The management VPC and both app clouds join the tailnet selected by this API credential.
resource "tailscale_acl" "this" {
  overwrite_existing_content = true
  acl = jsonencode({
    tagOwners     = merge({ for tag in local.router_tags : tag => ["autogroup:admin"] }, local.onprem_policy.tagOwners)
    autoApprovers = { routes = var.routes }
    acls = concat([
      { action = "accept", src = concat(local.router_tags, keys(var.routes)),
      dst = concat([for tag in local.router_tags : "${tag}:*"], [for cidr in keys(var.routes) : "${cidr}:*"]) },
      { action = "accept", src = ["autogroup:admin"], dst = ["*:*"] },
    ], local.onprem_policy.acls)
  })
}
resource "tailscale_dns_preferences" "this" { magic_dns = true }
resource "tailscale_tailnet_key" "router" {
  for_each      = local.router_roles
  reusable      = true
  ephemeral     = false
  preauthorized = true
  expiry        = 7776000
  description   = "${var.prefix}-${each.value}-router"
  tags          = ["tag:${each.value}"]
  depends_on    = [tailscale_acl.this]
}
resource "aws_secretsmanager_secret" "api" {
  name = "foxtrot/${var.prefix}/tailscale-api-token"
}
resource "aws_secretsmanager_secret_version" "api" {
  secret_id                = aws_secretsmanager_secret.api.id
  secret_string_wo         = var.api_key
  secret_string_wo_version = 1
}
resource "aws_secretsmanager_secret" "aws" {
  name = "foxtrot/${var.prefix}/tailscale-authkey-aws"
}
resource "aws_secretsmanager_secret_version" "aws" {
  secret_id     = aws_secretsmanager_secret.aws.id
  secret_string = tailscale_tailnet_key.router["aws"].key
}
resource "aws_secretsmanager_secret" "mgmt" {
  name = "foxtrot/${var.prefix}/tailscale-authkey-mgmt"
}
resource "aws_secretsmanager_secret_version" "mgmt" {
  secret_id     = aws_secretsmanager_secret.mgmt.id
  secret_string = tailscale_tailnet_key.router["mgmt"].key
}
resource "google_secret_manager_secret" "gcp" {
  secret_id = "foxtrot-${var.prefix}-tailscale-authkey-gcp"
  replication {
    auto {}
  }
}
resource "google_secret_manager_secret_version" "gcp" {
  secret      = google_secret_manager_secret.gcp.id
  secret_data = tailscale_tailnet_key.router["gcp"].key
}
output "auth_key_refs" {
  value = { aws = aws_secretsmanager_secret.aws.arn, gcp = google_secret_manager_secret.gcp.secret_id, mgmt = aws_secretsmanager_secret.mgmt.arn }
}
output "api_key_ref" { value = aws_secretsmanager_secret.api.arn }
