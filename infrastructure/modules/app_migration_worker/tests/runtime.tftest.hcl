mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"sts:AssumeRole\",\"Principal\":{\"Service\":\"ecs-tasks.amazonaws.com\"}}]}"
    }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
}

variables {
  app_id                     = "goods-a"
  cluster_id                 = "arn:aws:ecs:ap-northeast-2:123456789012:cluster/platform"
  subnet_ids                 = ["subnet-12345678"]
  security_group_ids         = ["sg-12345678"]
  namespace_id               = "ns-12345678"
  log_group                  = "/foxtrot/platform"
  region                     = "ap-northeast-2"
  db_image                   = "example/db:release"
  redis_data_image           = "example/redis-data:release"
  redis_api_image            = "example/redis-api:release"
  db_state_table             = { name = "foxtrot-db-operator", arn = "arn:aws:dynamodb:ap-northeast-2:123456789012:table/foxtrot-db-operator" }
  state_file_system_id       = "fs-12345678"
  db_runtime_secrets         = {}
  db_api_token_secret_arn    = "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:goods-a-db-123456"
  redis_api_token_secret_arn = "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:goods-a-redis-123456"
  redis_clients_secret_arn   = "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:goods-a-clients-123456"
  database_secret_arns       = []
  redis_secret_arns          = []
}

run "app_a" {
  command = plan
  assert {
    condition     = output.application.database.proxy.host == "db-goods-a.internal" && output.application.redis.proxy.host == "redis-goods-a.internal"
    error_message = "An application must receive dedicated, stable proxy addresses."
  }
  assert {
    condition     = output.application.dbNamespace == "app:goods-a" && aws_efs_access_point.redis.root_directory[0].path == "/applications/goods-a/redis"
    error_message = "DB control state and Redis files must be scoped to the application."
  }
}

run "app_b" {
  command = plan
  variables { app_id = "goods-b" }
  assert {
    condition     = output.application.database.proxy.host != run.app_a.application.database.proxy.host && output.application.redis.proxy.host != run.app_a.application.redis.proxy.host
    error_message = "Different applications must not share proxy addresses."
  }
  assert {
    condition     = output.application.dbNamespace != run.app_a.application.dbNamespace && aws_efs_access_point.redis.root_directory[0].path == "/applications/goods-b/redis"
    error_message = "Different applications must not share state namespaces or files."
  }
}

run "invalid_app_id" {
  command = plan
  variables { app_id = "../another-app" }
  expect_failures = [var.app_id]
}
