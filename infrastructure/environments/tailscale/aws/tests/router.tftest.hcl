mock_provider "aws" {
  mock_data "aws_vpc" {
    defaults = { cidr_block = "10.20.0.0/16" }
  }
  mock_data "aws_subnet" {
    defaults = { vpc_id = "vpc-0123456789abcdef0" }
  }
  mock_data "aws_route_table" {
    defaults = { vpc_id = "vpc-0123456789abcdef0" }
  }
}
variables {
  account_id                  = "123456789012"
  vpc_id                      = "vpc-0123456789abcdef0"
  subnet_id                   = "subnet-0123456789abcdef0"
  route_table_ids             = ["rtb-0123456789abcdef0", "rtb-0123456789abcdef1"]
  remote_cidrs                = ["10.40.0.0/24", "10.60.0.0/16"]
  database_security_group_ids = ["sg-0123456789abcdef0"]
  client_security_group_ids   = ["sg-0123456789abcdef1"]
  ami_id                      = "ami-0123456789abcdef0"
  auth_key_secret_arn         = "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:tailscale-test"
}
run "shared_router_and_return_routes" {
  command = plan
  assert {
    condition     = !aws_instance.router.source_dest_check && aws_instance.router.root_block_device[0].encrypted && aws_instance.router.metadata_options[0].http_tokens == "required"
    error_message = "The EC2 router must forward transit traffic and protect its node identity."
  }
  assert {
    condition     = length(aws_route.remote) == 4 && length(aws_vpc_security_group_ingress_rule.remote_database) == 2
    error_message = "Every selected client/DB route table needs remote subnet routes and explicit DB ingress."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.auth_key.policy).Statement[0].Resource == var.auth_key_secret_arn
    error_message = "The router can read only its configured auth secret."
  }
}
run "reject_router_in_other_vpc" {
  command = plan
  override_data {
    target = data.aws_subnet.router
    values = { vpc_id = "vpc-99999999999999999" }
  }
  expect_failures = [aws_instance.router]
}
run "restricted_client_egress" {
  command = plan
  assert {
    condition     = length(aws_vpc_security_group_egress_rule.remote_clients) == 2
    error_message = "Restricted clients need explicit remote DB egress as well as VPC routes."
  }
}
run "redis_uses_shared_router_without_opening_rds_ports" {
  command = plan
  variables {
    redis_ports              = [6378, 6379]
    cache_security_group_ids = ["sg-0123456789abcdef2"]
  }
  assert {
    condition     = length(aws_vpc_security_group_ingress_rule.clients) == 3 && length(aws_vpc_security_group_egress_rule.remote_clients) == 6 && length(aws_vpc_security_group_ingress_rule.remote_cache) == 4
    error_message = "Redis proxy/worker traffic and the return path need the selected Redis ports on the same router."
  }
  assert {
    condition     = alltrue([for rule in aws_vpc_security_group_ingress_rule.remote_database : rule.from_port == 5432 && rule.to_port == 5432]) && alltrue([for rule in aws_vpc_security_group_ingress_rule.remote_cache : contains([6378, 6379], rule.from_port)])
    error_message = "Redis ingress must be scoped to cache groups; RDS stays PostgreSQL-only."
  }
}
run "reject_unselected_cache_ports" {
  command = plan
  variables { cache_security_group_ids = ["sg-0123456789abcdef2"] }
  expect_failures = [var.cache_security_group_ids]
}
run "reject_non_redis_port" {
  command = plan
  variables { redis_ports = [22] }
  expect_failures = [var.redis_ports]
}

run "app_readiness_uses_explicit_remote_ports" {
  command = plan
  variables {
    app_ports              = [8000]
    app_security_group_ids = ["sg-0123456789abcdef3"]
    service_ports          = [6432, 8080]
  }
  assert {
    condition = length(aws_vpc_security_group_ingress_rule.remote_app) == 2 && alltrue([
      for rule in aws_vpc_security_group_ingress_rule.remote_app : rule.from_port == 8000 && contains(var.remote_cidrs, rule.cidr_ipv4)
    ])
    error_message = "Readiness ingress must target only the app group, its port and configured private source networks."
  }
  assert {
    condition     = alltrue([for rule in aws_vpc_security_group_ingress_rule.remote_database : rule.from_port == 5432])
    error_message = "App probes must not open extra database ports."
  }
}
