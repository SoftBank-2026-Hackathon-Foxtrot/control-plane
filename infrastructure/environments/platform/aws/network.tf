# Management VPC: public subnets hold the gateway, private subnets hold every ECS task.
resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = var.name }
}
resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = var.name }
}
resource "aws_subnet" "public" {
  count             = length(var.azs)
  vpc_id            = aws_vpc.this.id
  availability_zone = var.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index) # 10.30.0.0/24, 10.30.1.0/24
  tags              = { Name = "${var.name}-public-${count.index}", tier = "public" }
}
resource "aws_subnet" "private" {
  count             = length(var.azs)
  vpc_id            = aws_vpc.this.id
  availability_zone = var.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, 10 + count.index) # 10.30.10.0/24, 10.30.11.0/24
  tags              = { Name = "${var.name}-private-${count.index}", tier = "private" }
}
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.name}-public" }
}
resource "aws_route" "public_internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}
resource "aws_route_table_association" "public" {
  count          = length(var.azs)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# Private subnets leave through the gateway instance: the internet (NAT) and the app
# environments (Tailscale). The tailnet range itself also goes through it.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.name}-private" }
}
resource "aws_route" "private_default" {
  route_table_id         = aws_route_table.private.id
  destination_cidr_block = "0.0.0.0/0"
  network_interface_id   = aws_instance.gateway.primary_network_interface_id
}
resource "aws_route" "private_apps" {
  for_each               = toset(concat(var.app_cidrs, ["100.64.0.0/10"]))
  route_table_id         = aws_route_table.private.id
  destination_cidr_block = each.value
  network_interface_id   = aws_instance.gateway.primary_network_interface_id
}
resource "aws_route_table_association" "private" {
  count          = length(var.azs)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# Free gateway endpoints: state tables and S3 never cross the gateway instance.
resource "aws_vpc_endpoint" "gateway" {
  for_each          = toset(["dynamodb", "s3"])
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.region}.${each.value}"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id, aws_route_table.public.id]
  tags              = { Name = "${var.name}-${each.value}" }
}

# ECS tasks accept private app sources on the explicit proxy/adapter ports below.
# The gateway source-NATs app traffic today; with shared routers (SNAT disabled) sources arrive as is.
resource "aws_security_group" "tasks" {
  name_prefix = "${var.name}-tasks-"
  description = "Platform ECS tasks"
  vpc_id      = aws_vpc.this.id
  tags        = { Name = "${var.name}-tasks" }
}
resource "aws_vpc_security_group_ingress_rule" "tasks_from_vpc" {
  security_group_id = aws_security_group.tasks.id
  cidr_ipv4         = var.vpc_cidr
  ip_protocol       = "-1"
}
resource "aws_vpc_security_group_egress_rule" "tasks_all" {
  security_group_id = aws_security_group.tasks.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_vpc_security_group_ingress_rule" "tasks_from_apps" {
  for_each = { for pair in setproduct(toset([for cidr in keys(local.ts_routes) : cidr if cidr != var.vpc_cidr]), toset([6379, 6432, 7000, 8000, 8080])) :
  "${pair[0]}:${pair[1]}" => { cidr = pair[0], port = pair[1] } }
  security_group_id = aws_security_group.tasks.id
  cidr_ipv4         = each.value.cidr
  ip_protocol       = "tcp"
  from_port         = each.value.port
  to_port           = each.value.port
}
