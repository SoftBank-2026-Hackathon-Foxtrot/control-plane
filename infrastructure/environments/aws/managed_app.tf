module "app_ecs" {
  count                  = local.managed_app ? 1 : 0
  source                 = "../../modules/aws/app_ecs"
  name                   = var.deployment_id
  region                 = var.region
  vpc_id                 = module.vpc.vpc_id
  public_subnet_id       = module.vpc.public_subnet_id
  private_subnet_ids     = module.vpc.private_subnet_ids
  private_route_table_id = module.vpc.private_route_table_id
  security_group_id      = module.security_groups.app_id
  bucket_arn             = module.s3.bucket_arn
  tags                   = merge(module.metadata.common, { component = "compute" })
  app                    = var.app
  environment            = module.app_runtime[0].environment
  bootstrap_environment  = module.app_runtime[0].bootstrap_environment
  entrypoint             = var.app.entrypoint
  runtime_secrets = {
    APP_DB_PASSWORD   = aws_secretsmanager_secret.app_db.arn
    DB_ADMIN_USER     = "${module.rds.master_secret_arn}:username::"
    DB_ADMIN_PASSWORD = "${module.rds.master_secret_arn}:password::"
  }
  depends_on = [module.vpc, aws_secretsmanager_secret_version.app_db]
}
locals {
  managed_compute = {
    id            = try(module.app_ecs[0].service_id, null)
    public_ip     = null
    private_ip    = null
    identity      = try(module.app_ecs[0].task_role_arn, null)
    runtime       = "ECS_FARGATE"
    readiness_url = try("${module.app_ecs[0].url}${var.app.health_path}", null)
  }
  managed_app_handoff = {
    image       = try(var.app.image, null)
    url         = try(module.app_ecs[0].url, null)
    env_keys    = try(module.app_runtime[0].app_env_keys, [])
    boot_log    = try(module.app_ecs[0].log_group, null)
    health_path = try(var.app.health_path, null)
    runtime     = "ECS_FARGATE"
  }
}
