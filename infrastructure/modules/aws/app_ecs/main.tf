terraform {
  required_providers { aws = { source = "hashicorp/aws" } }
}
variable "name" { type = string }
variable "region" { type = string }
variable "vpc_id" { type = string }
variable "public_subnet_id" { type = string }
variable "private_subnet_ids" { type = list(string) }
variable "private_route_table_id" { type = string }
variable "security_group_id" { type = string }
variable "bucket_arn" { type = string }
variable "tags" { type = map(string) }
variable "app" { type = any }
variable "environment" { type = map(string) }
variable "bootstrap_environment" { type = map(string) }
variable "entrypoint" { type = list(string) }
variable "runtime_secrets" { type = map(string) }

resource "aws_ecs_cluster" "this" {
  name = var.name
  tags = var.tags
}
resource "aws_cloudwatch_log_group" "this" {
  name              = "/foxtrot/apps/${var.name}"
  retention_in_days = 14
  tags              = var.tags
}
resource "aws_service_discovery_private_dns_namespace" "this" {
  name = "${var.name}.app.internal"
  vpc  = var.vpc_id
}
# Image pulls, secret injection and external application calls use NAT. Private tasks have no public IP.
resource "aws_eip" "nat" { domain = "vpc" }
resource "aws_nat_gateway" "this" {
  allocation_id = aws_eip.nat.id
  subnet_id     = var.public_subnet_id
  tags          = var.tags
}
resource "aws_route" "egress" {
  route_table_id         = var.private_route_table_id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.this.id
}
# An app-specific private endpoint also lets the platform check this target without a shared public LB.
resource "aws_lb" "this" {
  name               = var.name
  internal           = true
  load_balancer_type = "application"
  subnets            = var.private_subnet_ids
  security_groups    = [var.security_group_id]
  tags               = var.tags
}
resource "aws_lb_target_group" "this" {
  name        = var.name
  vpc_id      = var.vpc_id
  target_type = "ip"
  port        = var.app.port
  protocol    = "HTTP"
  health_check {
    path    = var.app.health_path
    matcher = "200"
  }
}
resource "aws_lb_listener" "this" {
  load_balancer_arn = aws_lb.this.arn
  port              = var.app.port
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.this.arn
  }
}
resource "aws_vpc_security_group_ingress_rule" "load_balancer" {
  security_group_id            = var.security_group_id
  referenced_security_group_id = var.security_group_id
  ip_protocol                  = "tcp"
  from_port                    = var.app.port
  to_port                      = var.app.port
}
resource "aws_vpc_security_group_egress_rule" "load_balancer" {
  security_group_id            = var.security_group_id
  referenced_security_group_id = var.security_group_id
  ip_protocol                  = "tcp"
  from_port                    = var.app.port
  to_port                      = var.app.port
}
locals {
  base_env    = merge(var.environment, var.bootstrap_environment)
  app_secrets = merge(var.runtime_secrets, var.app.secret_env)
  api_env     = { for k, v in merge(local.base_env, var.app.api_env) : k => v if !contains(keys(local.app_secrets), k) }
  worker_env  = { for k, v in merge(local.base_env, var.app.worker_env) : k => v if !contains(keys(local.app_secrets), k) }
  containers = concat([
    { name  = "api", image = var.app.image, entryPoint = var.entrypoint, command = ["api"],
      ports = [var.app.port], environment = local.api_env, secrets = local.app_secrets,
    health = ["python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:${var.app.port}${var.app.health_path}', timeout=2)\""] }
    ], [for index in range(var.app.worker_count) : {
      name        = "worker-${index + 1}", image = var.app.image, entryPoint = var.entrypoint, command = ["w${index + 1}"],
      environment = local.worker_env, secrets = local.app_secrets,
      dependsOn   = [{ containerName = "api", condition = "HEALTHY" }]
    }], var.app.tunnel_secret == null ? [] : [
    { name = "cloudflared", image = var.app.cloudflared_image, command = ["tunnel", "--no-autoupdate", "run"],
    secrets = { TUNNEL_TOKEN = var.app.tunnel_secret }, dependsOn = [{ containerName = "api", condition = "HEALTHY" }] }
  ])
}
module "service" {
  source              = "../../platform_service"
  name                = var.name
  region              = var.region
  cluster_id          = aws_ecs_cluster.this.id
  subnet_ids          = var.private_subnet_ids
  security_group_ids  = [var.security_group_id]
  namespace_id        = aws_service_discovery_private_dns_namespace.this.id
  log_group           = aws_cloudwatch_log_group.this.name
  cpu                 = var.app.cpu * 1024
  memory              = var.app.memory_mib
  containers          = local.containers
  load_balancer       = { target_group_arn = aws_lb_target_group.this.arn, container_name = "api", container_port = var.app.port }
  task_policy_enabled = true
  task_policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:ListBucket", "s3:GetBucketLocation"], Resource = var.bucket_arn },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], Resource = "${var.bucket_arn}/*" }
  ] })
  depends_on = [aws_route.egress, aws_lb_listener.this]
}
output "service_id" { value = module.service.service_arn }
output "task_role_arn" { value = module.service.task_role_arn }
output "url" { value = "http://${aws_lb.this.dns_name}:${var.app.port}" }
output "log_group" { value = aws_cloudwatch_log_group.this.name }
