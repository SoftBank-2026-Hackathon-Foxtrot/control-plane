resource "aws_security_group" "app" {
  name_prefix = "${var.name}-app-"
  description = "Public PoC web entry; SSH only from explicit admin CIDRs"
  vpc_id      = var.vpc_id
  tags        = merge(var.tags, { Name = "${var.name}-app", exposure = "public" })
}
resource "aws_security_group" "db" {
  name_prefix = "${var.name}-db-"
  description = "PostgreSQL from app security group only"
  vpc_id      = var.vpc_id
  tags        = merge(var.tags, { Name = "${var.name}-db", exposure = "private" })
}
resource "aws_security_group" "cache" {
  name_prefix = "${var.name}-cache-"
  description = "Redis TLS from app security group only"
  vpc_id      = var.vpc_id
  tags        = merge(var.tags, { Name = "${var.name}-cache", exposure = "private" })
}
locals {
  web_rules = { for pair in setproduct(var.web_ingress_cidrs, var.web_ports) : "${pair[0]}:${pair[1]}" => pair }
}
resource "aws_vpc_security_group_ingress_rule" "web" {
  for_each          = local.web_rules
  security_group_id = aws_security_group.app.id
  cidr_ipv4         = each.value[0]
  ip_protocol       = "tcp"
  from_port         = each.value[1]
  to_port           = each.value[1]
  tags              = var.tags
}
resource "aws_vpc_security_group_ingress_rule" "ssh" {
  for_each          = var.admin_cidrs
  security_group_id = aws_security_group.app.id
  cidr_ipv4         = each.value
  ip_protocol       = "tcp"
  from_port         = 22
  to_port           = 22
  tags              = var.tags
}
resource "aws_vpc_security_group_ingress_rule" "db" {
  security_group_id            = aws_security_group.db.id
  referenced_security_group_id = aws_security_group.app.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  tags                         = var.tags
}
resource "aws_vpc_security_group_ingress_rule" "cache" {
  security_group_id            = aws_security_group.cache.id
  referenced_security_group_id = aws_security_group.app.id
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  tags                         = var.tags
}
resource "aws_vpc_security_group_egress_rule" "https" {
  security_group_id = aws_security_group.app.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  tags              = var.tags
}
resource "aws_vpc_security_group_egress_rule" "db" {
  security_group_id            = aws_security_group.app.id
  referenced_security_group_id = aws_security_group.db.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  tags                         = var.tags
}
resource "aws_vpc_security_group_egress_rule" "cache" {
  security_group_id            = aws_security_group.app.id
  referenced_security_group_id = aws_security_group.cache.id
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  tags                         = var.tags
}

locals {
  proxy_rules = { for pair in setproduct(var.proxy_cidrs, [6379, 6432]) : "${pair[0]}:${pair[1]}" => pair }
}
resource "aws_vpc_security_group_egress_rule" "proxy" {
  for_each          = local.proxy_rules
  security_group_id = aws_security_group.app.id
  cidr_ipv4         = each.value[0]
  ip_protocol       = "tcp"
  from_port         = each.value[1]
  to_port           = each.value[1]
  tags              = var.tags
}
resource "aws_vpc_security_group_ingress_rule" "migration_db" {
  for_each          = var.migration_source_cidrs
  security_group_id = aws_security_group.db.id
  cidr_ipv4         = each.value
  ip_protocol       = "tcp"
  from_port         = 5432
  to_port           = 5432
  tags              = var.tags
}
resource "aws_vpc_security_group_ingress_rule" "migration_cache" {
  for_each          = var.migration_source_cidrs
  security_group_id = aws_security_group.cache.id
  cidr_ipv4         = each.value
  ip_protocol       = "tcp"
  from_port         = 6379
  to_port           = 6379
  tags              = var.tags
}
resource "aws_vpc_security_group_egress_rule" "cloudflare_tunnel" {
  for_each          = var.enable_cloudflare_tunnel ? toset(["tcp", "udp"]) : toset([])
  security_group_id = aws_security_group.app.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = each.value
  from_port         = 7844
  to_port           = 7844
  tags              = var.tags
}

# Package mirrors are served over plain HTTP on some distributions.
resource "aws_vpc_security_group_egress_rule" "http" {
  security_group_id = aws_security_group.app.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  tags              = var.tags
}
# Cross-cloud replication: the target database (for example Cloud SQL) connects to this one.
resource "aws_vpc_security_group_ingress_rule" "replication_db" {
  for_each          = var.replication_cidrs
  security_group_id = aws_security_group.db.id
  cidr_ipv4         = each.value
  ip_protocol       = "tcp"
  from_port         = 5432
  to_port           = 5432
  tags              = var.tags
}

# Tailscale direct connections (it falls back to relays over 443 without this).
resource "aws_vpc_security_group_egress_rule" "tailscale" {
  for_each          = var.enable_tailscale ? toset(["41641", "3478"]) : toset([])
  security_group_id = aws_security_group.app.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "udp"
  from_port         = tonumber(each.value)
  to_port           = tonumber(each.value)
  tags              = var.tags
}
