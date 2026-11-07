mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"sts:AssumeRole\",\"Principal\":{\"Service\":\"ecs-tasks.amazonaws.com\"}}]}" }
  }
  mock_resource "aws_lb" {
    defaults = { arn = "arn:aws:elasticloadbalancing:ap-northeast-2:123456789012:loadbalancer/app/test/1234567890abcdef", dns_name = "internal-test-123.ap-northeast-2.elb.amazonaws.com" }
  }
  mock_resource "aws_lb_target_group" {
    defaults = { arn = "arn:aws:elasticloadbalancing:ap-northeast-2:123456789012:targetgroup/test/1234567890abcdef" }
  }
  mock_resource "aws_ecs_cluster" {
    defaults = { id = "arn:aws:ecs:ap-northeast-2:123456789012:cluster/test" }
  }
  mock_resource "aws_ecs_task_definition" {
    defaults = { arn = "arn:aws:ecs:ap-northeast-2:123456789012:task-definition/test:1" }
  }
  mock_resource "aws_service_discovery_service" {
    defaults = { arn = "arn:aws:servicediscovery:ap-northeast-2:123456789012:service/srv-test" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::test-poc-bucket", id = "test-poc-bucket" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/test-app" }
  }
  mock_resource "aws_db_instance" {
    defaults = { address = "test-db.abc.ap-northeast-2.rds.amazonaws.com", port = 5432, master_user_secret = [{ secret_arn = "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:rds-master", kms_key_id = "", secret_status = "active" }] }
  }
  mock_resource "aws_secretsmanager_secret" {
    defaults = { arn = "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:app-db" }
  }
  mock_data "aws_secretsmanager_secret_version" {
    defaults = { secret_string = "{\"username\":\"platformadmin\",\"password\":\"test-password\"}" }
  }
  mock_resource "aws_elasticache_replication_group" {
    defaults = { primary_endpoint_address = "test-cache.abc.apn2.cache.amazonaws.com", port = 6379 }
  }
}
run "root_wiring" {
  command = apply
  variables {
    deployment_id             = "fox-aws-poc01"
    metadata                  = { "project" = "foxtrot", "environment" = "poc", "owner" = "platform-team", "cost_center" = "hackathon", "workload" = "web", "migration_id" = "mig-001", "data_class" = "internal", "expires_on" = "2026-12-01" }
    region                    = "ap-northeast-2"
    account_id                = "123456789012"
    bucket_name               = "replace-with-globally-unique-aws-bucket"
    database_name             = "app"
    web_ingress_cidrs         = ["0.0.0.0/0"]
    web_ports                 = [80, 443]
    admin_cidrs               = []
    deletion_protection       = true
    final_snapshot_identifier = "fox-aws-poc01-final-unique"
    network                   = { "vpc_cidr" = "10.10.0.0/16", "public_subnet_cidr" = "10.10.0.0/24", "private_subnet_cidrs" = ["10.10.10.0/24", "10.10.11.0/24"], "availability_zones" = ["ap-northeast-2a", "ap-northeast-2c"] }
    sizing                    = { "compute_instance_type" = "t3.small", "compute_disk_gib" = 30, "db_instance_class" = "db.t3.micro", "db_storage_gib" = 20, "db_max_storage_gib" = 100, "db_multi_az" = false, "cache_node_type" = "cache.t3.micro", "cache_node_count" = 1 }
    ami_id                    = "ami-1234567890abcdef0"
    app = { image = "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/chiikawa-groupbuy:test", entrypoint = ["python", "-m", "app.main"] }
  }
  assert {
    condition     = !strcontains(module.app_runtime[0].script, "init_schema") && !strcontains(module.app_runtime[0].script, "/seed?")
    error_message = "App schema creation and seed data belong to the user app, not IaC."
  }
  assert {
    condition     = output.handoff.cloud == "aws" && output.handoff.schema_version == 1
    error_message = "AWS handoff contract must remain stable."
  }
  assert {
    condition     = output.handoff.compute.runtime == "ECS_FARGATE"  && length(module.app_ecs) == 1
    error_message = "User apps must run on private Fargate tasks, not an application VM."
  }
  assert {
    condition = length(var.app.entrypoint) > 0
    error_message = "The platform supplies the application container entrypoint."
  }
  assert {
    condition     = output.handoff.database.version == "17" && output.handoff.database.logical_replication && !output.handoff.database.publicly_accessible && !output.handoff.cache.tls_required
    error_message = "PG17 with logical replication, private DB and a plain-RESP cache (Redis proxy) are the defaults."
  }
}
run "tailscale_makes_the_vm_a_router" {
  command = apply
  variables {
    deployment_id             = "fox-aws-poc01"
    metadata                  = { "project" = "foxtrot", "environment" = "poc", "owner" = "platform-team", "cost_center" = "hackathon", "workload" = "web", "migration_id" = "mig-001", "data_class" = "internal", "expires_on" = "2026-12-01" }
    region                    = "ap-northeast-2"
    account_id                = "123456789012"
    bucket_name               = "replace-with-globally-unique-aws-bucket"
    final_snapshot_identifier = "fox-aws-poc01-final-unique"
    network                   = { "vpc_cidr" = "10.10.0.0/16", "public_subnet_cidr" = "10.10.0.0/24", "private_subnet_cidrs" = ["10.10.10.0/24", "10.10.11.0/24"], "availability_zones" = ["ap-northeast-2a", "ap-northeast-2c"] }
    ami_id                    = "ami-1234567890abcdef0"
    app = { image = "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/chiikawa-groupbuy:test", entrypoint = ["python", "-m", "app.main"] }
    tailscale                 = { auth_key_secret = "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:foxtrot/tailscale-authkey-aws" }
  }
  assert {
    condition     = !strcontains(module.app_runtime[0].script, "tailscale up")  && output.router_inputs.vpc_id == module.vpc.vpc_id
    error_message = "Managed apps use a separately registered subnet router, never a Tailscale sidecar."
  }
}
run "reject_non_registry_image" {
  command = plan
  variables {
    deployment_id             = "fox-aws-poc01"
    metadata                  = { "project" = "foxtrot", "environment" = "poc", "owner" = "platform-team", "cost_center" = "hackathon", "workload" = "web", "migration_id" = "mig-001", "data_class" = "internal", "expires_on" = "2026-12-01" }
    region                    = "ap-northeast-2"
    account_id                = "123456789012"
    bucket_name               = "replace-with-globally-unique-aws-bucket"
    final_snapshot_identifier = "fox-aws-poc01-final-unique"
    network                   = { "vpc_cidr" = "10.10.0.0/16", "public_subnet_cidr" = "10.10.0.0/24", "private_subnet_cidrs" = ["10.10.10.0/24", "10.10.11.0/24"], "availability_zones" = ["ap-northeast-2a", "ap-northeast-2c"] }
    ami_id                    = "ami-1234567890abcdef0"
    app = { image = "docker.io/library/nginx:latest", entrypoint = ["python", "-m", "app.main"] }
  }
  expect_failures = [var.app]
}
run "reject_open_ssh" {
  command = plan
  variables {
    deployment_id             = "fox-aws-poc01"
    metadata                  = { "project" = "foxtrot", "environment" = "poc", "owner" = "platform-team", "cost_center" = "hackathon", "workload" = "web", "migration_id" = "mig-001", "data_class" = "internal", "expires_on" = "2026-12-01" }
    region                    = "ap-northeast-2"
    account_id                = "123456789012"
    bucket_name               = "replace-with-globally-unique-aws-bucket"
    database_name             = "app"
    web_ingress_cidrs         = ["0.0.0.0/0"]
    web_ports                 = [80, 443]
    admin_cidrs               = ["0.0.0.0/0"]
    deletion_protection       = true
    final_snapshot_identifier = "fox-aws-poc01-final-unique"
    network                   = { "vpc_cidr" = "10.10.0.0/16", "public_subnet_cidr" = "10.10.0.0/24", "private_subnet_cidrs" = ["10.10.10.0/24", "10.10.11.0/24"], "availability_zones" = ["ap-northeast-2a", "ap-northeast-2c"] }
    sizing                    = { "compute_instance_type" = "t3.small", "compute_disk_gib" = 30, "db_instance_class" = "db.t3.micro", "db_storage_gib" = 20, "db_max_storage_gib" = 100, "db_multi_az" = false, "cache_node_type" = "cache.t3.micro", "cache_node_count" = 1 }
    ami_id                    = "ami-1234567890abcdef0"
    app = { image = "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/chiikawa-groupbuy:test", entrypoint = ["python", "-m", "app.main"] }
    key_name                  = "existing-key"
  }
  expect_failures = [var.admin_cidrs]
}
run "reject_database_public_port" {
  command = plan
  variables {
    deployment_id             = "fox-aws-poc01"
    metadata                  = { "project" = "foxtrot", "environment" = "poc", "owner" = "platform-team", "cost_center" = "hackathon", "workload" = "web", "migration_id" = "mig-001", "data_class" = "internal", "expires_on" = "2026-12-01" }
    region                    = "ap-northeast-2"
    account_id                = "123456789012"
    bucket_name               = "replace-with-globally-unique-aws-bucket"
    database_name             = "app"
    web_ingress_cidrs         = ["0.0.0.0/0"]
    web_ports                 = [5432]
    admin_cidrs               = []
    deletion_protection       = true
    final_snapshot_identifier = "fox-aws-poc01-final-unique"
    network                   = { "vpc_cidr" = "10.10.0.0/16", "public_subnet_cidr" = "10.10.0.0/24", "private_subnet_cidrs" = ["10.10.10.0/24", "10.10.11.0/24"], "availability_zones" = ["ap-northeast-2a", "ap-northeast-2c"] }
    sizing                    = { "compute_instance_type" = "t3.small", "compute_disk_gib" = 30, "db_instance_class" = "db.t3.micro", "db_storage_gib" = 20, "db_max_storage_gib" = 100, "db_multi_az" = false, "cache_node_type" = "cache.t3.micro", "cache_node_count" = 1 }
    ami_id                    = "ami-1234567890abcdef0"
    app = { image = "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/chiikawa-groupbuy:test", entrypoint = ["python", "-m", "app.main"] }
  }
  expect_failures = [var.web_ports]
}
run "reject_unreviewed_size" {
  command = plan
  variables {
    deployment_id             = "fox-aws-poc01"
    metadata                  = { "project" = "foxtrot", "environment" = "poc", "owner" = "platform-team", "cost_center" = "hackathon", "workload" = "web", "migration_id" = "mig-001", "data_class" = "internal", "expires_on" = "2026-12-01" }
    region                    = "ap-northeast-2"
    account_id                = "123456789012"
    bucket_name               = "replace-with-globally-unique-aws-bucket"
    database_name             = "app"
    web_ingress_cidrs         = ["0.0.0.0/0"]
    web_ports                 = [80, 443]
    admin_cidrs               = []
    deletion_protection       = true
    final_snapshot_identifier = "fox-aws-poc01-final-unique"
    network                   = { "vpc_cidr" = "10.10.0.0/16", "public_subnet_cidr" = "10.10.0.0/24", "private_subnet_cidrs" = ["10.10.10.0/24", "10.10.11.0/24"], "availability_zones" = ["ap-northeast-2a", "ap-northeast-2c"] }
    sizing                    = { "compute_instance_type" = "p5.48xlarge" }
    ami_id                    = "ami-1234567890abcdef0"
    app = { image = "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/chiikawa-groupbuy:test", entrypoint = ["python", "-m", "app.main"] }
  }
  expect_failures = [var.sizing]
}
run "private_database" {
  command = plan
  module { source = "../../modules/aws/rds" }
  variables {
    name                      = "test-db"
    tags                      = { "project" = "foxtrot", "environment" = "poc", "owner" = "platform-team", "cost_center" = "hackathon", "workload" = "web", "migration_id" = "mig-001", "data_class" = "internal", "expires_on" = "2026-12-01", "deployment_id" = "test-poc", "managed_by" = "terraform", "template_version" = "v0-1", "security_profile" = "public-app-private-data-v1" }
    subnet_ids                = ["subnet-123", "subnet-456"]
    security_group_id         = "sg-123"
    instance_class            = "db.t3.micro"
    storage_gib               = 20
    max_storage_gib           = 100
    multi_az                  = false
    engine_version            = "17"
    database_name             = "app"
    deletion_protection       = true
    final_snapshot_identifier = "test-final"
  }
  assert {
    condition     = aws_db_instance.this.publicly_accessible == false && aws_db_instance.this.storage_encrypted
    error_message = "RDS must stay private and encrypted."
  }
  assert {
    condition     = aws_db_instance.this.manage_master_user_password && aws_db_instance.this.deletion_protection && !aws_db_instance.this.skip_final_snapshot
    error_message = "RDS credentials and deletion controls must not regress."
  }
  assert {
    condition     = aws_db_parameter_group.this.family == "postgres17" && { for p in aws_db_parameter_group.this.parameter : p.name => p.value }["rds.logical_replication"] == "1"
    error_message = "The DB migration agent needs logical replication on PostgreSQL 17."
  }
}
run "private_storage" {
  command = plan
  module { source = "../../modules/aws/s3" }
  variables {
    bucket_name = "test-poc-bucket"
    tags        = { "project" = "foxtrot", "environment" = "poc", "owner" = "platform-team", "cost_center" = "hackathon", "workload" = "web", "migration_id" = "mig-001", "data_class" = "internal", "expires_on" = "2026-12-01", "deployment_id" = "test-poc", "managed_by" = "terraform", "template_version" = "v0-1", "security_profile" = "public-app-private-data-v1" }
  }
  assert {
    condition     = aws_s3_bucket_public_access_block.this.block_public_policy && aws_s3_bucket_public_access_block.this.restrict_public_buckets
    error_message = "Bucket must not be public."
  }
  assert {
    condition     = aws_s3_bucket.this.force_destroy == false && aws_s3_bucket_versioning.this.versioning_configuration[0].status == "Enabled"
    error_message = "Versioning and nonempty-bucket deletion protection are required."
  }
}
run "cache_tls_and_auth_together" {
  command = plan
  module { source = "../../modules/aws/elasticache" }
  variables {
    name               = "test-cache"
    tags               = { "project" = "foxtrot", "environment" = "poc", "owner" = "platform-team", "cost_center" = "hackathon", "workload" = "web", "migration_id" = "mig-001", "data_class" = "internal", "expires_on" = "2026-12-01", "deployment_id" = "test-poc", "managed_by" = "terraform", "template_version" = "v0-1", "security_profile" = "public-app-private-data-v1" }
    subnet_ids         = ["subnet-123", "subnet-456"]
    security_group_id  = "sg-123"
    node_type          = "cache.t3.micro"
    node_count         = 2
    transit_encryption = true
    auth_token         = "TESTONLYnotarealsecret123456789"
  }
  assert {
    condition     = aws_elasticache_replication_group.this.transit_encryption_enabled && aws_elasticache_replication_group.this.at_rest_encryption_enabled && aws_elasticache_replication_group.this.auth_token_update_strategy == "SET"
    error_message = "Redis encryption/auth cannot be relaxed."
  }
  assert {
    condition     = aws_elasticache_replication_group.this.automatic_failover_enabled && aws_elasticache_replication_group.this.multi_az_enabled
    error_message = "Multiple nodes must enable failover."
  }
  assert {
    condition     = { for p in aws_elasticache_parameter_group.migration.parameter : p.name => p.value } == { "notify-keyspace-events" = "KEA", "maxmemory-policy" = "noeviction" } && aws_elasticache_replication_group.this.parameter_group_name == aws_elasticache_parameter_group.migration.name
    error_message = "Migration requires attached keyspace notifications and noeviction."
  }

}

run "cache_plain_resp_by_default" {
  command = plan
  module { source = "../../modules/aws/elasticache" }
  variables {
    name              = "test-cache"
    tags              = {}
    subnet_ids        = ["subnet-123", "subnet-456"]
    security_group_id = "sg-123"
    node_type         = "cache.t3.micro"
    node_count        = 1
  }
  assert {
    condition     = !aws_elasticache_replication_group.this.transit_encryption_enabled && aws_elasticache_replication_group.this.at_rest_encryption_enabled
    error_message = "The team's Redis proxy speaks plain RESP; at-rest encryption stays on."
  }
}
run "cache_tls_needs_token" {
  command = plan
  module { source = "../../modules/aws/elasticache" }
  variables {
    name               = "test-cache"
    tags               = {}
    subnet_ids         = ["subnet-123", "subnet-456"]
    security_group_id  = "sg-123"
    node_type          = "cache.t3.micro"
    node_count         = 1
    transit_encryption = true
  }
  expect_failures = [aws_elasticache_replication_group.this]
}

run "management_enabled" {
  command = plan
  module { source = "../../modules/aws/security_groups" }
  variables {
    name                     = "test-poc"
    web_ingress_cidrs        = ["0.0.0.0/0"]
    web_ports                = [80, 443]
    admin_cidrs              = []
    tags                     = {}
    vpc_id                   = "vpc-123"
    proxy_cidrs              = ["10.50.0.10/32"]
    enable_cloudflare_tunnel = true
    migration_source_cidrs   = ["10.20.10.5/32"]
  }
  assert {
    condition     = length(aws_vpc_security_group_egress_rule.proxy) == 2 && aws_vpc_security_group_egress_rule.proxy["10.50.0.10/32:6432"].to_port == 6432 && aws_vpc_security_group_ingress_rule.migration_db["10.20.10.5/32"].from_port == 5432 && aws_vpc_security_group_ingress_rule.migration_cache["10.20.10.5/32"].to_port == 6379 && length(aws_vpc_security_group_egress_rule.cloudflare_tunnel) == 2
    error_message = "Management paths must be explicit, port-scoped and opt-in."
  }
}

run "management_default_closed" {
  command = plan
  module { source = "../../modules/aws/security_groups" }
  variables {
    name              = "test-poc"
    web_ingress_cidrs = ["0.0.0.0/0"]
    web_ports         = [80, 443]
    admin_cidrs       = []
    tags              = {}
    vpc_id            = "vpc-123"
  }
  assert {
    condition     = length(aws_vpc_security_group_egress_rule.proxy) == 0 && length(aws_vpc_security_group_ingress_rule.migration_db) == 0 && length(aws_vpc_security_group_ingress_rule.migration_cache) == 0 && length(aws_vpc_security_group_egress_rule.cloudflare_tunnel) == 0
    error_message = "Management paths must be explicit, port-scoped and opt-in."
  }
}

run "management_reject_public" {
  command = plan
  module { source = "../../modules/aws/security_groups" }
  variables {
    name                     = "test-poc"
    web_ingress_cidrs        = ["0.0.0.0/0"]
    web_ports                = [80, 443]
    admin_cidrs              = []
    tags                     = {}
    vpc_id                   = "vpc-123"
    proxy_cidrs              = ["0.0.0.0/0"]
    enable_cloudflare_tunnel = true
  }
  expect_failures = [var.proxy_cidrs]
}

run "legacy_infrastructure_only" {
  command = apply
  variables {
    deployment_id             = "fox-aws-poc01"
    metadata                  = { "project" = "foxtrot", "environment" = "poc", "owner" = "platform-team", "cost_center" = "hackathon", "workload" = "web", "migration_id" = "mig-001", "data_class" = "internal", "expires_on" = "2026-12-01" }
    region                    = "ap-northeast-2"
    account_id                = "123456789012"
    bucket_name               = "replace-with-globally-unique-aws-bucket"
    database_name             = "app"
    web_ingress_cidrs         = ["0.0.0.0/0"]
    web_ports                 = [80, 443]
    admin_cidrs               = []
    deletion_protection       = true
    final_snapshot_identifier = "fox-aws-poc01-final-unique"
    network                   = { "vpc_cidr" = "10.10.0.0/16", "public_subnet_cidr" = "10.10.0.0/24", "private_subnet_cidrs" = ["10.10.10.0/24", "10.10.11.0/24"], "availability_zones" = ["ap-northeast-2a", "ap-northeast-2c"] }
    sizing                    = { "compute_instance_type" = "t3.small", "compute_disk_gib" = 30, "db_instance_class" = "db.t3.micro", "db_storage_gib" = 20, "db_max_storage_gib" = 100, "db_multi_az" = false, "cache_node_type" = "cache.t3.micro", "cache_node_count" = 1 }
    ami_id                    = "ami-1234567890abcdef0"
    redis_auth_token          = "TESTONLYlegacytoken12345678"
  }
  assert {
    condition     = output.handoff.app == null && output.handoff.cache.tls_required && output.handoff.database.version == "16"
    error_message = "Legacy inputs retain infrastructure-only deployment, PG16 and cache TLS."
  }
}
