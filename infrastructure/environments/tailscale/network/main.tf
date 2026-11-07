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
provider "tailscale" { tailnet = "-" }

# This root has its own backend and API credential. It never uses the platform tailnet state.
resource "tailscale_acl" "this" {
  overwrite_existing_content = true
  acl = jsonencode({
    tagOwners     = { "tag:aws" = ["autogroup:admin"], "tag:gcp" = ["autogroup:admin"] }
    autoApprovers = { routes = var.routes }
    acls = [
      { action = "accept", src = concat(["tag:aws", "tag:gcp"], keys(var.routes)),
      dst = concat(["tag:aws:*", "tag:gcp:*"], [for cidr in keys(var.routes) : "${cidr}:*"]) },
      { action = "accept", src = ["autogroup:admin"], dst = ["*:*"] },
    ]
  })
}
resource "tailscale_dns_preferences" "this" { magic_dns = true }
resource "tailscale_tailnet_key" "router" {
  for_each      = toset(["aws", "gcp"])
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
  value = { aws = aws_secretsmanager_secret.aws.arn, gcp = google_secret_manager_secret.gcp.secret_id }
}
output "api_key_ref" { value = aws_secretsmanager_secret.api.arn }
