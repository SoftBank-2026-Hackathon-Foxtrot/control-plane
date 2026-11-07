# One platform service: a single-task Fargate service (the services keep single-writer state),
# its containers, secrets from Secrets Manager, EFS mounts, a task role and a *.internal name.
terraform {
  required_providers {
    aws = { source = "hashicorp/aws" }
  }
}

variable "name" { type = string }
variable "cluster_id" { type = string }
variable "subnet_ids" { type = list(string) }
variable "security_group_ids" { type = list(string) }
variable "namespace_id" { type = string }
variable "log_group" { type = string }
variable "region" { type = string }
variable "cpu" {
  type    = number
  default = 512
}
variable "memory" {
  type    = number
  default = 1024
}
variable "desired_count" {
  type    = number
  default = 1
}
variable "load_balancer" {
  type    = object({ target_group_arn = string, container_name = string, container_port = number })
  default = null
}
variable "efs" {
  description = "volume name => { file_system_id, access_point_id }"
  type        = map(object({ file_system_id = string, access_point_id = string }))
  default     = {}
}
variable "bind_volumes" {
  description = "task-local volumes shared between the containers of one task"
  type        = list(string)
  default     = []
}
variable "task_policy" {
  description = "IAM policy JSON for the task role (null: none)"
  type        = string
  default     = null
}
variable "task_policy_enabled" {
  description = "Set when the policy JSON includes resource attributes unknown during the initial plan."
  type        = bool
  default     = null
}
variable "containers" {
  type = list(object({
    name        = string
    image       = string
    essential   = optional(bool, true)
    command     = optional(list(string))
    entryPoint  = optional(list(string))
    ports       = optional(list(number), [])
    environment = optional(map(string), {})
    secrets     = optional(map(string), {}) # ENV NAME => Secrets Manager ARN
    mounts      = optional(map(string), {}) # volume name => container path
    health      = optional(list(string))    # CMD-SHELL command
    dependsOn   = optional(list(object({ containerName = string, condition = string })), [])
  }))
}

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

locals {
  secret_arns = distinct([for arn in flatten([for c in var.containers : values(c.secrets)]) :
  join(":", slice(split(":", arn), 0, min(7, length(split(":", arn)))))])
  has_secrets = length(flatten([for c in var.containers : keys(c.secrets)])) > 0 # known at plan time
}

# Execution role: pull images, write logs, inject this service's secrets.
resource "aws_iam_role" "execution" {
  name               = "foxtrot-${var.name}-exec"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}
resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}
resource "aws_iam_role_policy" "execution_secrets" {
  count = local.has_secrets ? 1 : 0
  name  = "secrets"
  role  = aws_iam_role.execution.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = local.secret_arns }]
  })
}

# Task role: what the service itself may call.
resource "aws_iam_role" "task" {
  name               = "foxtrot-${var.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}
resource "aws_iam_role_policy" "task" {
  count  = var.task_policy_enabled == null ? (var.task_policy == null ? 0 : 1) : (var.task_policy_enabled ? 1 : 0)
  name   = "service"
  role   = aws_iam_role.task.id
  policy = var.task_policy
}
resource "aws_iam_role_policy" "task_efs" {
  count = length(var.efs) > 0 ? 1 : 0
  name  = "efs"
  role  = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow", Action = ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"]
      Resource = "*", Condition = { StringEquals = { "elasticfilesystem:AccessPointArn" = [for v in values(var.efs) : "arn:aws:elasticfilesystem:${var.region}:${data.aws_caller_identity.current.account_id}:access-point/${v.access_point_id}"] } }
    }]
  })
}
data "aws_caller_identity" "current" {}

resource "aws_ecs_task_definition" "this" {
  family                   = "foxtrot-${var.name}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  dynamic "volume" {
    for_each = toset(var.bind_volumes)
    content {
      name = volume.value
    }
  }
  dynamic "volume" {
    for_each = var.efs
    content {
      name = volume.key
      efs_volume_configuration {
        file_system_id     = volume.value.file_system_id
        transit_encryption = "ENABLED"
        authorization_config {
          access_point_id = volume.value.access_point_id
          iam             = "ENABLED"
        }
      }
    }
  }
  container_definitions = jsonencode([for c in var.containers : merge(
    {
      name         = c.name
      image        = c.image
      essential    = c.essential
      portMappings = [for p in c.ports : { containerPort = p, protocol = "tcp" }]
      environment  = [for k in sort(keys(c.environment)) : { name = k, value = c.environment[k] }]
      secrets      = [for k in sort(keys(c.secrets)) : { name = k, valueFrom = c.secrets[k] }]
      mountPoints  = [for v, path in c.mounts : { sourceVolume = v, containerPath = path, readOnly = false }]
      dependsOn    = c.dependsOn
      logConfiguration = {
        logDriver = "awslogs"
        options   = { "awslogs-group" = var.log_group, "awslogs-region" = var.region, "awslogs-stream-prefix" = var.name }
      }
    },
    c.command == null ? {} : { command = c.command },
    c.entryPoint == null ? {} : { entryPoint = c.entryPoint },
    c.health == null ? {} : { healthCheck = { command = concat(["CMD-SHELL"], c.health), interval = 15, timeout = 5, retries = 3, startPeriod = 60 } },
  )])
}

resource "aws_service_discovery_service" "this" {
  name = var.name
  dns_config {
    namespace_id   = var.namespace_id
    routing_policy = "MULTIVALUE"
    dns_records {
      type = "A"
      ttl  = 10
    }
  }
}

resource "aws_ecs_service" "this" {
  name                               = var.name
  cluster                            = var.cluster_id
  task_definition                    = aws_ecs_task_definition.this.arn
  desired_count                      = var.desired_count
  launch_type                        = "FARGATE"
  enable_execute_command             = true
  deployment_minimum_healthy_percent = 0 # single writer: stop the old task before the new one
  deployment_maximum_percent         = 100
  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = var.security_group_ids
    assign_public_ip = false
  }
  service_registries {
    registry_arn = aws_service_discovery_service.this.arn
  }
  dynamic "load_balancer" {
    for_each = var.load_balancer == null ? [] : [var.load_balancer]
    content {
      target_group_arn = load_balancer.value.target_group_arn
      container_name   = load_balancer.value.container_name
      container_port   = load_balancer.value.container_port
    }
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = false
  }
}

resource "aws_iam_role_policy" "task_exec_command" {
  name = "ecs-exec"
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow", Resource = "*", Action = [
      "ssmmessages:CreateControlChannel", "ssmmessages:CreateDataChannel", "ssmmessages:OpenControlChannel", "ssmmessages:OpenDataChannel"
    ] }]
  })
}

output "task_role_arn" { value = aws_iam_role.task.arn }
output "task_role_name" { value = aws_iam_role.task.name }
output "service_arn" { value = aws_ecs_service.this.id }
output "dns_name" { value = "${var.name}.internal" }
