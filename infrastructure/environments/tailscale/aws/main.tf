terraform {
  required_version = ">= 1.10.0, < 2.0.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
  default_tags { tags = merge(var.tags, { component = "tailscale-router", managed_by = "terraform" }) }
}

data "aws_vpc" "network" { id = var.vpc_id }
data "aws_subnet" "router" { id = var.subnet_id }
data "aws_route_table" "clients" {
  for_each       = var.route_table_ids
  route_table_id = each.value
}
locals {
  forwarding_ports = setunion(toset([5432]), var.redis_ports, var.app_ports, var.service_ports)
}

module "bootstrap" {
  source            = "../../../modules/tailscale_bootstrap"
  name              = var.name
  cloud             = "aws"
  region            = var.region
  secret_ref        = var.auth_key_secret_arn
  advertised_cidrs  = [data.aws_vpc.network.cidr_block]
  remote_cidrs      = var.remote_cidrs
  tailscale_version = var.tailscale_version
}

resource "aws_iam_role" "router" {
  name = var.name
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "ec2.amazonaws.com" } }]
  })
}
resource "aws_iam_role_policy" "auth_key" {
  role = aws_iam_role.router.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = var.auth_key_secret_arn }
      ], var.secret_kms_key_arn == null ? [] : [
      { Effect = "Allow", Action = ["kms:Decrypt"], Resource = var.secret_kms_key_arn }
    ])
  })
}
resource "aws_iam_instance_profile" "router" {
  name = var.name
  role = aws_iam_role.router.name
}
resource "aws_iam_role_policy_attachment" "ssm" {
  count      = var.enable_ssm ? 1 : 0
  role       = aws_iam_role.router.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_security_group" "router" {
  name        = var.name
  description = "DB site-to-site forwarding and authenticated Tailscale transport"
  vpc_id      = var.vpc_id
}
resource "aws_vpc_security_group_ingress_rule" "wireguard" {
  security_group_id = aws_security_group.router.id
  ip_protocol       = "udp"
  from_port         = 41641
  to_port           = 41641
  cidr_ipv4         = "0.0.0.0/0"
}
resource "aws_vpc_security_group_ingress_rule" "clients" {
  for_each          = toset([for port in local.forwarding_ports : tostring(port)])
  security_group_id = aws_security_group.router.id
  ip_protocol       = "tcp"
  from_port         = tonumber(each.value)
  to_port           = tonumber(each.value)
  cidr_ipv4         = data.aws_vpc.network.cidr_block
}
resource "aws_vpc_security_group_egress_rule" "web" {
  for_each          = toset(["80", "443"])
  security_group_id = aws_security_group.router.id
  ip_protocol       = "tcp"
  from_port         = tonumber(each.value)
  to_port           = tonumber(each.value)
  cidr_ipv4         = "0.0.0.0/0"
}
resource "aws_vpc_security_group_egress_rule" "udp" {
  security_group_id = aws_security_group.router.id
  ip_protocol       = "udp"
  from_port         = 0
  to_port           = 65535
  cidr_ipv4         = "0.0.0.0/0"
}
resource "aws_vpc_security_group_egress_rule" "database" {
  for_each          = toset([for port in local.forwarding_ports : tostring(port)])
  security_group_id = aws_security_group.router.id
  ip_protocol       = "tcp"
  from_port         = tonumber(each.value)
  to_port           = tonumber(each.value)
  cidr_ipv4         = data.aws_vpc.network.cidr_block
}

resource "aws_instance" "router" {
  ami                         = var.ami_id
  instance_type               = var.instance_type
  subnet_id                   = var.subnet_id
  vpc_security_group_ids      = [aws_security_group.router.id]
  associate_public_ip_address = true
  source_dest_check           = false
  iam_instance_profile        = aws_iam_instance_profile.router.name
  user_data                   = module.bootstrap.startup_script
  user_data_replace_on_change = true
  credit_specification { cpu_credits = "standard" }
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  root_block_device {
    volume_type = "gp3"
    volume_size = 10
    encrypted   = true
  }
  tags       = { Name = var.name }
  depends_on = [aws_iam_role_policy.auth_key]
  lifecycle {
    precondition {
      condition     = data.aws_subnet.router.vpc_id == var.vpc_id && alltrue([for rt in data.aws_route_table.clients : rt.vpc_id == var.vpc_id])
      error_message = "The router subnet and every client route table must belong to the selected VPC."
    }
  }
}

resource "aws_route" "remote" {
  for_each = {
    for pair in setproduct(var.route_table_ids, var.remote_cidrs) :
    "${pair[0]}:${pair[1]}" => { table = pair[0], cidr = pair[1] }
  }
  route_table_id         = each.value.table
  destination_cidr_block = each.value.cidr
  network_interface_id   = aws_instance.router.primary_network_interface_id
}

resource "aws_vpc_security_group_ingress_rule" "remote_database" {
  for_each = {
    for pair in setproduct(var.database_security_group_ids, var.remote_cidrs) :
    "${pair[0]}:${pair[1]}" => { group = pair[0], cidr = pair[1] }
  }
  security_group_id = each.value.group
  ip_protocol       = "tcp"
  from_port         = 5432
  to_port           = 5432
  cidr_ipv4         = each.value.cidr
}

resource "aws_vpc_security_group_egress_rule" "remote_clients" {
  for_each = {
    for pair in setproduct(var.client_security_group_ids, var.remote_cidrs, local.forwarding_ports) :
    "${pair[0]}:${pair[1]}:${pair[2]}" => { group = pair[0], cidr = pair[1], port = pair[2] }
  }
  security_group_id = each.value.group
  ip_protocol       = "tcp"
  from_port         = each.value.port
  to_port           = each.value.port
  cidr_ipv4         = each.value.cidr
}

resource "aws_vpc_security_group_ingress_rule" "remote_cache" {
  for_each = {
    for pair in setproduct(var.cache_security_group_ids, var.remote_cidrs, var.redis_ports) :
    "${pair[0]}:${pair[1]}:${pair[2]}" => { group = pair[0], cidr = pair[1], port = pair[2] }
  }
  security_group_id = each.value.group
  ip_protocol       = "tcp"
  from_port         = each.value.port
  to_port           = each.value.port
  cidr_ipv4         = each.value.cidr
}

output "instance_id" { value = aws_instance.router.id }
output "private_ip" { value = aws_instance.router.private_ip }
output "advertised_cidrs" { value = [data.aws_vpc.network.cidr_block] }
output "routing_scope" {
  value = { route_table_ids = var.route_table_ids, remote_cidrs = var.remote_cidrs, db_port = 5432, redis_ports = var.redis_ports }
}

resource "aws_vpc_security_group_ingress_rule" "remote_app" {
  for_each = { for pair in setproduct(var.app_security_group_ids, var.remote_cidrs, var.app_ports) :
  "${pair[0]}:${pair[1]}:${pair[2]}" => { group = pair[0], cidr = pair[1], port = pair[2] } }
  security_group_id = each.value.group
  ip_protocol       = "tcp"
  from_port         = each.value.port
  to_port           = each.value.port
  cidr_ipv4         = each.value.cidr
}
