# The private network between the platform and the app environments: tailnet policy, split DNS
# for *.internal, and one reusable, pre-approved auth key per role, written straight into the
# secret slots the gateway and the shared cloud routers read at boot.

locals {
  ts_tags = { mgmt = "tag:mgmt", aws = "tag:aws", gcp = "tag:gcp" }
  ts_routes = {
    (var.vpc_cidr)                                                                                                                                       = ["tag:mgmt"]
    (local.provisioner_baseline.aws.baseline.network.vpc_cidr)                                                                                           = ["tag:aws"]
    (local.provisioner_baseline.gcp.baseline.network.subnet_cidr)                                                                                        = ["tag:gcp"]
    ("${local.provisioner_baseline.gcp.baseline.network.service_range_address}/${local.provisioner_baseline.gcp.baseline.network.service_range_prefix}") = ["tag:gcp"]
  }
}

resource "tailscale_acl" "this" {
  overwrite_existing_content = true
  acl = jsonencode({
    tagOwners = { for t in values(local.ts_tags) : t => ["autogroup:admin"] }
    acls = [
      # Platform and app environments reach each other and the VPC ranges they advertise.
      # Sources include the advertised ranges: a platform task (10.30.x) keeps its own address
      # through the gateway, and the receiving router checks that address against this rule.
      { action = "accept", src = concat(values(local.ts_tags), keys(local.ts_routes)),
      dst = concat([for t in values(local.ts_tags) : "${t}:*"], [for r in keys(local.ts_routes) : "${r}:*"]) },
      { action = "accept", src = ["autogroup:admin"], dst = ["*:*"] },
    ]
    autoApprovers = { routes = local.ts_routes }
  })
}

resource "tailscale_dns_preferences" "this" {
  magic_dns = true
}
resource "tailscale_dns_split_nameservers" "internal" {
  domain      = "internal"
  nameservers = [cidrhost(var.vpc_cidr, 2)] # the VPC resolver, reached through the gateway's route
}

resource "tailscale_tailnet_key" "role" {
  for_each      = local.ts_tags
  reusable      = true # a replaced VM joins again with the same key
  ephemeral     = false
  preauthorized = true
  expiry        = 7776000 # 90 days
  description   = "foxtrot-${each.key}"
  tags          = [each.value]
  depends_on    = [tailscale_acl.this]
}

# Gateway (tag:mgmt): this root owns the secret. Kept the same secret as the old hand-filled slot.
resource "aws_secretsmanager_secret" "tailscale_mgmt" {
  name                    = "foxtrot/platform/tailscale-authkey-mgmt"
  description             = "Tailscale auth key (tag:mgmt) for the platform gateway; written by Terraform"
  recovery_window_in_days = 0
}
resource "aws_secretsmanager_secret_version" "tailscale_mgmt" {
  secret_id     = aws_secretsmanager_secret.tailscale_mgmt.id
  secret_string = tailscale_tailnet_key.role["mgmt"].key
}
moved {
  from = aws_secretsmanager_secret.external["tailscale-authkey-mgmt"]
  to   = aws_secretsmanager_secret.tailscale_mgmt
}
moved {
  from = aws_secretsmanager_secret_version.external["tailscale-authkey-mgmt"]
  to   = aws_secretsmanager_secret_version.tailscale_mgmt
}

# Shared cloud routers: the slots come from environments/foundation/*; this root fills them.
data "aws_secretsmanager_secret" "tailscale_aws" {
  name = "foxtrot/tailscale-authkey-aws"
}
resource "aws_secretsmanager_secret_version" "tailscale_aws" {
  secret_id     = data.aws_secretsmanager_secret.tailscale_aws.id
  secret_string = tailscale_tailnet_key.role["aws"].key
}
resource "google_secret_manager_secret_version" "tailscale_gcp" {
  secret      = "projects/${var.gcp_project_id}/secrets/foxtrot-tailscale-authkey-gcp"
  secret_data = tailscale_tailnet_key.role["gcp"].key
}
