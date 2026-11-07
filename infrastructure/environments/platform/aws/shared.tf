# Shared by every platform service: cluster, service discovery, file storage, images, state
# tables, logs and secrets.

resource "aws_ecs_cluster" "this" {
  name = var.name
  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

# *.internal names for every service. App VMs resolve them through Tailscale split DNS to the
# VPC resolver (10.30.0.2), reached through the gateway's subnet route.
resource "aws_service_discovery_private_dns_namespace" "internal" {
  name        = "internal"
  description = "Foxtrot platform services"
  vpc         = aws_vpc.this.id
}

resource "aws_cloudwatch_log_group" "platform" {
  name              = "/foxtrot/platform"
  retention_in_days = 14
}

# ---- File storage for single-writer state (Redis control files, traffic SQLite, provisioner) ----
resource "aws_efs_file_system" "state" {
  encrypted        = true
  performance_mode = "generalPurpose"
  throughput_mode  = "bursting"
  tags             = { Name = "${var.name}-state" }
}
resource "aws_security_group" "efs" {
  name_prefix = "${var.name}-efs-"
  description = "NFS from platform tasks"
  vpc_id      = aws_vpc.this.id
}
resource "aws_vpc_security_group_ingress_rule" "efs_from_tasks" {
  security_group_id            = aws_security_group.efs.id
  referenced_security_group_id = aws_security_group.tasks.id
  ip_protocol                  = "tcp"
  from_port                    = 2049
  to_port                      = 2049
}
resource "aws_efs_mount_target" "state" {
  count           = length(var.azs)
  file_system_id  = aws_efs_file_system.state.id
  subnet_id       = aws_subnet.private[count.index].id
  security_groups = [aws_security_group.efs.id]
}
locals {
  # directory => owner uid (each service writes only its own directory)
  efs_dirs = { redis = 0, traffic = 10001, provisioner = 10001 }
}
resource "aws_efs_access_point" "dir" {
  for_each       = local.efs_dirs
  file_system_id = aws_efs_file_system.state.id
  posix_user {
    uid = each.value
    gid = each.value
  }
  root_directory {
    path = "/${each.key}"
    creation_info {
      owner_uid   = each.value
      owner_gid   = each.value
      permissions = "0750"
    }
  }
  tags = { Name = "${var.name}-${each.key}" }
}

# ---- Platform images ----
locals {
  repositories = ["control-plane", "db-migration-adaptor", "db-pgbouncer", "redis-proxy", "redis-statemig",
  "object-migration", "traffic", "provisioner", "platform-console"]
  image = { for r in local.repositories : r => "${aws_ecr_repository.platform[r].repository_url}:${lookup(var.image_tags, r, "latest")}" }
}
resource "aws_ecr_repository" "platform" {
  for_each             = toset(local.repositories)
  name                 = "foxtrot/${each.value}"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration { scan_on_push = true }
}
resource "aws_ecr_lifecycle_policy" "platform" {
  for_each   = aws_ecr_repository.platform
  repository = each.value.name
  policy = jsonencode({ rules = [{
    rulePriority = 1, description = "keep the 10 most recent images"
    selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
    action       = { type = "expire" }
  }] })
}

# ---- State tables (one per service, the team's rule) ----
resource "aws_dynamodb_table" "pk_sk" {
  for_each     = toset(["foxtrot-control-plane", "foxtrot-object-migration"])
  name         = each.value
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"
  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
  dynamic "ttl" {
    for_each = each.value == "foxtrot-control-plane" ? [1] : []
    content {
      attribute_name = "expiresAt"
      enabled        = true
    }
  }
  point_in_time_recovery { enabled = true }
}
resource "aws_dynamodb_table" "db_operator" {
  name         = "foxtrot-db-operator"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "namespace"
  range_key    = "record"
  attribute {
    name = "namespace"
    type = "S"
  }
  attribute {
    name = "record"
    type = "S"
  }
  point_in_time_recovery { enabled = true }
}

# ---- Secrets ----
# Tokens between platform services: generated here.
locals {
  generated = ["db-adapter-token", "redis-admin-token", "object-adapter-token", "traffic-service-token",
  "workload-token", "provisioner-token", "observation-token", "db-pgbouncer-admin-password"]
  # Values that come from other systems: created with the placeholder UNSET; owners overwrite
  # them (Terraform ignores later changes). Services treat UNSET as "not configured".
  external = {
    "cloudflare-api-token" = "Cloudflare API token for the traffic service (LB edit, pool health read)"
    "github-client-secret" = "GitHub OAuth app secret for console login"
  }
}
resource "random_password" "generated" {
  for_each = toset(local.generated)
  length   = 40
  special  = false
}
resource "aws_secretsmanager_secret" "generated" {
  for_each                = toset(local.generated)
  name                    = "foxtrot/platform/${each.value}"
  recovery_window_in_days = 0
}
resource "aws_secretsmanager_secret_version" "generated" {
  for_each      = toset(local.generated)
  secret_id     = aws_secretsmanager_secret.generated[each.value].id
  secret_string = random_password.generated[each.value].result
}
resource "aws_secretsmanager_secret" "external" {
  for_each                = local.external
  name                    = "foxtrot/platform/${each.key}"
  description             = each.value
  recovery_window_in_days = 0
}
resource "aws_secretsmanager_secret_version" "external" {
  for_each      = local.external
  secret_id     = aws_secretsmanager_secret.external[each.key].id
  secret_string = "UNSET"
  lifecycle {
    ignore_changes = [secret_string]
  }
}
# The platform tunnel's token comes straight from Cloudflare (cloudflare.tf).
resource "aws_secretsmanager_secret" "tunnel_platform" {
  name                    = "foxtrot/platform/cloudflare-tunnel-platform"
  description             = "Cloudflare tunnel token for the platform (console); written by Terraform"
  recovery_window_in_days = 0
}
resource "aws_secretsmanager_secret_version" "tunnel_platform" {
  secret_id     = aws_secretsmanager_secret.tunnel_platform.id
  secret_string = data.cloudflare_zero_trust_tunnel_cloudflared_token.platform.token
}

locals {
  secret_arn = merge(
    { for k, s in aws_secretsmanager_secret.generated : k => s.arn },
    { for k, s in aws_secretsmanager_secret.external : k => s.arn },
    { "cloudflare-tunnel-platform" = aws_secretsmanager_secret.tunnel_platform.arn },
    { "tailscale-authkey-mgmt" = aws_secretsmanager_secret.tailscale_mgmt.arn },
    { for k, v in aws_secretsmanager_secret.redis_endpoints : k => v.arn },
  )
}
