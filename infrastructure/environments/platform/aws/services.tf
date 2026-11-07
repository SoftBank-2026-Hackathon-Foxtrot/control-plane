# The platform services, one Fargate service each, addressed as <name>.internal.

locals {
  svc = {
    cluster_id         = aws_ecs_cluster.this.id
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.tasks.id]
    namespace_id       = aws_service_discovery_private_dns_namespace.internal.id
    log_group          = aws_cloudwatch_log_group.platform.name
    region             = var.region
  }
  otel_env = var.observability == null ? {} : {
    OTEL_EXPORTER_OTLP_ENDPOINT = var.observability.endpoint
    OTEL_EXPORTER_OTLP_PROTOCOL = "http/protobuf"
  }
  otel_secrets = var.observability == null ? {} : {
    OTEL_EXPORTER_OTLP_HEADERS = var.observability.headers_secret_arn
  }
  db_namespace = "managed-apps"
  table_arn    = { for k, t in aws_dynamodb_table.pk_sk : k => t.arn }
}

# ---------------------------------------------------------------------------------------------
# DB migration adaptor: API + worker (backend) and PgBouncer (pooler) in one task, registered as
# db-proxy.internal: 6432 PgBouncer for app VMs, 8080 HTTPS API for the control-plane. (Cloud Map
# gives an ECS service one name, and its zone takes no extra records.)
locals {
  db_runtime = {
    "db-runtime-users"       = jsonencode({ pooler_admin = random_password.generated["db-pgbouncer-admin-password"].result })
    "db-runtime-admin-dsn"   = "postgresql://pooler_admin:${random_password.generated["db-pgbouncer-admin-password"].result}@127.0.0.1:6432/pgbouncer?sslmode=verify-full&sslrootcert=/run/foxtrot/client.crt"
    "db-runtime-client-cert" = "${tls_locally_signed_cert.db.cert_pem}${local.ca_pem}"
    "db-runtime-client-key"  = tls_private_key.db.private_key_pem
    "db-runtime-server-ca"   = file("${path.module}/rds-ca-bundle.pem") # ap-northeast-2 RDS CAs (the global bundle exceeds the 64 KB secret limit)
  }
  db_registry = jsonencode({ databases = {}, bindings = {}, workloads = {}, tables = [], sequences = [] })
  db_env = {
    DB_OPERATOR_STATE_TABLE        = aws_dynamodb_table.db_operator.name
    DB_OPERATOR_STATE_NAMESPACE    = local.db_namespace
    AWS_DEFAULT_REGION             = var.region
    DB_OPERATOR_REGISTRY           = "/run/foxtrot/registry.json"
    DB_OPERATOR_REGISTRY_JSON      = local.db_registry
    DB_OPERATOR_BINDINGS_ENABLED   = "true"
    DB_OPERATOR_POOLER_DIRECTORY   = "/run/foxtrot"
    DB_OPERATOR_PUBLIC_HOST        = "db-proxy.internal"
    DB_OPERATOR_PUBLIC_PORT        = "6432"
    DB_OPERATOR_RUNTIME_CLIENT_TLS = "require"
    DB_OPERATOR_RUNTIME_SERVER_TLS = "verify-full"
    DB_OPERATOR_RUNTIME_ADMIN_USER = "pooler_admin"
  }
  db_secrets = {
    DB_OPERATOR_API_TOKEN                      = local.secret_arn["db-adapter-token"]
    DB_OPERATOR_RUNTIME_USERS_JSON             = aws_secretsmanager_secret.db_runtime["db-runtime-users"].arn
    PGBOUNCER_ADMIN_DSN                        = aws_secretsmanager_secret.db_runtime["db-runtime-admin-dsn"].arn
    DB_OPERATOR_RUNTIME_CLIENT_CERTIFICATE_PEM = aws_secretsmanager_secret.db_runtime["db-runtime-client-cert"].arn
    DB_OPERATOR_RUNTIME_CLIENT_KEY_PEM         = aws_secretsmanager_secret.db_runtime["db-runtime-client-key"].arn
    DB_OPERATOR_RUNTIME_SERVER_CA_PEM          = aws_secretsmanager_secret.db_runtime["db-runtime-server-ca"].arn
  }
}
resource "aws_secretsmanager_secret" "db_runtime" {
  for_each                = toset(keys(local.db_runtime))
  name                    = "foxtrot/platform/${each.key}"
  recovery_window_in_days = 0
}
resource "aws_secretsmanager_secret_version" "db_runtime" {
  for_each      = toset(keys(local.db_runtime))
  secret_id     = aws_secretsmanager_secret.db_runtime[each.key].id
  secret_string = local.db_runtime[each.key]
}

module "db_migration" {
  source = "../../../modules/platform_service"
  name   = "db-proxy"
  cpu    = 512
  memory = 1024
  containers = [
    {
      name        = "pooler"
      image       = local.image["db-migration-adaptor"]
      entryPoint  = ["foxtrot-db-runtime"]
      command     = ["pooler"]
      ports       = [6432]
      environment = merge(local.db_env, local.otel_env)
      secrets     = merge(local.db_secrets, local.otel_secrets)
      mounts      = { runtime = "/run/foxtrot" }
      health      = ["test -f /run/foxtrot/pooler.ready && pg_isready -q -h 127.0.0.1 -p 6432 -d pgbouncer -U pooler_admin -t 3"]
    },
    {
      name        = "backend"
      image       = local.image["db-migration-adaptor"]
      entryPoint  = ["foxtrot-db-runtime"]
      command     = ["backend"]
      ports       = [8080]
      environment = merge(local.db_env, local.otel_env)
      secrets     = merge(local.db_secrets, local.otel_secrets, { DB_OPERATOR_RUNTIME_GCP_CREDENTIALS_JSON = aws_secretsmanager_secret.gcp["db"].arn })
      mounts      = { runtime = "/run/foxtrot" }
      health      = ["curl --insecure --fail --silent --max-time 3 https://127.0.0.1:8080/live"]
      dependsOn   = [{ containerName = "pooler", condition = "HEALTHY" }]
    },
  ]
  bind_volumes = ["runtime"]
  task_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Resource = aws_dynamodb_table.db_operator.arn, Action = [
        "dynamodb:DescribeTable", "dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem",
      "dynamodb:DeleteItem", "dynamodb:ConditionCheckItem"] },
      # Per-database credentials registered in the catalog (secret_ref).
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = "arn:aws:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:foxtrot/*" },
    ]
  })
  depends_on = [aws_secretsmanager_secret_version.db_runtime, aws_secretsmanager_secret_version.generated]
  # shared settings
  cluster_id         = local.svc.cluster_id
  subnet_ids         = local.svc.subnet_ids
  security_group_ids = local.svc.security_group_ids
  namespace_id       = local.svc.namespace_id
  log_group          = local.svc.log_group
  region             = local.svc.region
}

# ---------------------------------------------------------------------------------------------
# Redis: RESP proxy (redis-proxy.internal:6379, admin 7000), sync worker (7100) and statemig
# (redis-migrator.internal:8090). Off until the app Redis endpoints exist (see redis_enabled).
locals {
  redis_count = var.redis_enabled ? 1 : 0
  redis_token = { STATEMIG_ADMIN_TOKEN = local.secret_arn["redis-admin-token"] }
}
module "redis_proxy" {
  source        = "../../../modules/platform_service"
  name          = "redis-proxy"
  cpu           = 512
  memory        = 1024
  desired_count = local.redis_count
  efs           = { state = { file_system_id = aws_efs_file_system.state.id, access_point_id = aws_efs_access_point.dir["redis"].id } }
  containers = [{
    name        = "proxy"
    image       = local.image["redis-proxy"]
    entryPoint  = ["statemig-proxy"]
    ports       = [6379, 7000]
    environment = { STATEMIG_CONTROL_FILE = "/state/control.json" }
    secrets     = merge(local.redis_token, { STATEMIG_ENDPOINTS = local.secret_arn["redis-endpoints-proxy"] })
    mounts      = { state = "/state" }
  }]
  cluster_id         = local.svc.cluster_id
  subnet_ids         = local.svc.subnet_ids
  security_group_ids = local.svc.security_group_ids
  namespace_id       = local.svc.namespace_id
  log_group          = local.svc.log_group
  region             = local.svc.region
}
module "redis_syncworker" {
  source        = "../../../modules/platform_service"
  name          = "redis-syncworker"
  cpu           = 512
  memory        = 1024
  desired_count = local.redis_count
  containers = [{
    name       = "syncworker"
    image      = local.image["redis-proxy"]
    entryPoint = ["statemig-syncworker"]
    ports      = [7100]
    secrets    = merge(local.redis_token, { STATEMIG_ENDPOINTS = local.secret_arn["redis-endpoints-admin"] })
  }]
  cluster_id         = local.svc.cluster_id
  subnet_ids         = local.svc.subnet_ids
  security_group_ids = local.svc.security_group_ids
  namespace_id       = local.svc.namespace_id
  log_group          = local.svc.log_group
  region             = local.svc.region
}
module "redis_migrator" {
  source        = "../../../modules/platform_service"
  name          = "redis-migrator"
  cpu           = 256
  memory        = 512
  desired_count = local.redis_count
  efs           = { state = { file_system_id = aws_efs_file_system.state.id, access_point_id = aws_efs_access_point.dir["redis"].id } }
  containers = [{
    name    = "statemig"
    image   = local.image["redis-statemig"]
    command = ["uvicorn", "statemig.api:app", "--host", "0.0.0.0", "--port", "8090", "--timeout-keep-alive", "120"]
    ports   = [8090]
    environment = merge(local.otel_env, {
      STATEMIG_CONTROL_FILE = "/state/control.json"
      STATEMIG_PROXY        = "http://redis-proxy.internal:7000"
      STATEMIG_SYNCWORKER   = "http://redis-syncworker.internal:7100"
    })
    secrets = merge(local.redis_token, local.otel_secrets, { STATEMIG_ENDPOINTS = local.secret_arn["redis-endpoints-admin"] })
    mounts  = { state = "/state" }
  }]
  cluster_id         = local.svc.cluster_id
  subnet_ids         = local.svc.subnet_ids
  security_group_ids = local.svc.security_group_ids
  namespace_id       = local.svc.namespace_id
  log_group          = local.svc.log_group
  region             = local.svc.region
}

# ---------------------------------------------------------------------------------------------
# Object storage migration: object-migration.internal:8080
module "object_migration" {
  source = "../../../modules/platform_service"
  name   = "object-migration"
  cpu    = 1024
  memory = 2048
  containers = [{
    name  = "server"
    image = local.image["object-migration"]
    ports = [8080]
    environment = merge(local.otel_env, {
      DYNAMODB_TABLE   = aws_dynamodb_table.pk_sk["foxtrot-object-migration"].name
      DYNAMODB_REGION  = var.region
      ENGINE           = "rclone"
      GCP_PROJECT      = var.gcp_project_id
      OBJECT_RESOURCES = var.object_resources
    })
    secrets = merge(local.otel_secrets, { API_TOKEN = local.secret_arn["object-adapter-token"] })
  }]
  task_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Resource = local.table_arn["foxtrot-object-migration"], Action = [
      "dynamodb:DescribeTable", "dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Scan"] },
      # Buckets of app environments (foxtrot-*) on the AWS side.
      { Effect = "Allow", Resource = ["arn:aws:s3:::foxtrot-*", "arn:aws:s3:::foxtrot-*/*"], Action = [
      "s3:ListBucket", "s3:GetBucketLocation", "s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:GetBucketNotification", "s3:PutBucketNotification"] },
      { Effect = "Allow", Resource = "*", Action = ["sqs:CreateQueue", "sqs:DeleteQueue", "sqs:GetQueueAttributes", "sqs:SetQueueAttributes", "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueUrl"] },
    ]
  })
  cluster_id         = local.svc.cluster_id
  subnet_ids         = local.svc.subnet_ids
  security_group_ids = local.svc.security_group_ids
  namespace_id       = local.svc.namespace_id
  log_group          = local.svc.log_group
  region             = local.svc.region
}

# ---------------------------------------------------------------------------------------------
# Traffic service (Cloudflare LB weights): traffic.internal:8001
module "traffic" {
  source = "../../../modules/platform_service"
  name   = "traffic"
  cpu    = 256
  memory = 512
  efs    = { data = { file_system_id = aws_efs_file_system.state.id, access_point_id = aws_efs_access_point.dir["traffic"].id } }
  containers = [{
    name  = "server"
    image = local.image["traffic"]
    ports = [8001]
    environment = merge(local.otel_env, {
      CF_ZONE_ID      = var.traffic.zone_id
      CF_ACCOUNT_ID   = var.traffic.account_id
      CF_LB_ID        = var.traffic.lb_id
      CF_POOL_IDS     = var.traffic.pool_ids
      TRAFFIC_DB_PATH = "/var/lib/foxtrot/traffic/traffic.db"
    })
    secrets = merge(local.otel_secrets, { TRAFFIC_SERVICE_TOKEN = local.secret_arn["traffic-service-token"], CF_API_TOKEN = local.secret_arn["cloudflare-api-token"] })
    mounts  = { data = "/var/lib/foxtrot/traffic" }
    health  = ["python -c \"import urllib.request as u; u.urlopen('http://127.0.0.1:8001/health', timeout=2)\""]
  }]
  cluster_id         = local.svc.cluster_id
  subnet_ids         = local.svc.subnet_ids
  security_group_ids = local.svc.security_group_ids
  namespace_id       = local.svc.namespace_id
  log_group          = local.svc.log_group
  region             = local.svc.region
}

# ---------------------------------------------------------------------------------------------
# Application deployments run inside the control-plane backend.
locals {
  provisioner_config = jsonencode({
    workRoot            = "/var/lib/foxtrot-provisioner"
    stateBackend        = { bucket = "foxtrot-provisioner-state-${data.aws_caller_identity.current.account_id}", region = var.region }
    deletionProtection  = false
    forceDestroyStorage = true
    expiresInDays       = 30
    # Apps reach Redis and the object route through the platform (DB through PgBouncer comes next).
    appEnv = {
      REDIS_URL               = "redis://redis-proxy.internal:6379"
      OBJECT_MIGRATION_SERVER = "http://object-migration.internal:8080"
    }
    clouds = {
      aws = merge(local.provisioner_baseline.aws, {
        accounts      = [data.aws_caller_identity.current.account_id]
        images        = { "chiikawa-groupbuy" = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.region}.amazonaws.com/chiikawa-groupbuy:latest" }
        imagePrefixes = ["${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.region}.amazonaws.com/"]
        tailscale     = { authKeySecret = data.aws_secretsmanager_secret.tailscale_aws.arn, tag = "tag:aws" }
      })
      gcp = merge(local.provisioner_baseline.gcp, {
        projects      = [var.gcp_project_id]
        images        = { "chiikawa-groupbuy" = "asia-northeast3-docker.pkg.dev/${var.gcp_project_id}/apps/chiikawa-groupbuy:latest" }
        imagePrefixes = ["asia-northeast3-docker.pkg.dev/${var.gcp_project_id}/"]
        tailscale     = { authKeySecret = "projects/${var.gcp_project_id}/secrets/foxtrot-tailscale-authkey-gcp", tag = "tag:gcp" }
      })
    }
  })
  # Network plan and sizing baseline from provisioner/config.example.json (no account IDs there).
  provisioner_baseline = { for c, v in jsondecode(file("${path.module}/../../../provisioner/config.example.json")).clouds :
  c => { for k, x in v : k => x if !contains(["accounts", "projects", "images", "imagePrefixes"], k) } }
}

# ---------------------------------------------------------------------------------------------
# Control-plane (platform backend): control-plane.internal:8000
module "control_plane" {
  source = "../../../modules/platform_service"
  name   = "control-plane"
  cpu    = 1024
  memory = 2048
  efs = { work = { file_system_id = aws_efs_file_system.state.id, access_point_id = aws_efs_access_point.dir["provisioner"].id } }
  containers = [{
    name  = "server"
    image = local.image["control-plane"]
    ports = [8000]
    # The image carries no seed; write the seed and the internal CA from the environment first.
    entryPoint = ["sh", "-c"]
    command    = ["printf '%s' \"$CONTROL_PLANE_SEED_YAML\" > /tmp/seed.yaml && printf '%s' \"$FOXTROT_CA_PEM\" > /tmp/foxtrot-ca.pem && exec control-plane"]
    environment = merge(local.otel_env, {
      PROVISIONER_CONFIG_JSON = local.provisioner_config
      FOXTROT_INFRASTRUCTURE_ROOT = "/srv/infrastructure"
      TF_PLUGIN_CACHE_DIR = "/var/lib/foxtrot-provisioner/plugin-cache"
      CONTROL_PLANE_ENVIRONMENT              = "production"
      CONTROL_PLANE_OBSERVATION_TOKEN_SECRET = "env:OBSERVATION_TOKEN"
      CONTROL_PLANE_SEED_PATH                = "/tmp/seed.yaml"
      CONTROL_PLANE_SEED_YAML                = local.control_seed_yaml
      FOXTROT_CA_PEM                         = local.ca_pem
      CONTROL_PLANE_TABLE_NAME               = aws_dynamodb_table.pk_sk["foxtrot-control-plane"].name
      CONTROL_PLANE_REGION                   = var.region
      CONTROL_PLANE_DB_GATE_TABLE            = aws_dynamodb_table.db_operator.name
      CONTROL_PLANE_DB_GATE_NAMESPACE        = local.control_db_namespace
      CONTROL_PLANE_TRAFFIC_URL              = "http://traffic.internal:8001"
      CONTROL_PLANE_PUBLIC_URL               = var.console_url
      CONTROL_PLANE_ALLOWED_ORIGINS          = jsonencode([var.console_url])
      CONTROL_PLANE_GITHUB_CLIENT_ID         = var.github_client_id
      CONTROL_PLANE_GITHUB_ALLOWED_IDS       = jsonencode(var.github_allowed_ids)
      }, var.observability == null ? {} : {
      CONTROL_PLANE_PROMETHEUS_URL = var.observability.prometheus_url
      CONTROL_PLANE_TEMPO_URL      = var.observability.tempo_url
    })
    secrets = merge(local.otel_secrets, {
      GOOGLE_CREDENTIALS = aws_secretsmanager_secret.gcp["provisioner"].arn
      OBSERVATION_TOKEN     = local.secret_arn["observation-token"]
      TRAFFIC_SERVICE_TOKEN = local.secret_arn["traffic-service-token"]
      WORKLOAD_TOKEN        = local.secret_arn["workload-token"]
      GITHUB_CLIENT_SECRET  = local.secret_arn["github-client-secret"]
      DB_ADAPTER_TOKEN      = local.control_db_token
      REDIS_ADAPTER_TOKEN   = local.control_redis_token
      OBJECT_ADAPTER_TOKEN  = local.secret_arn["object-adapter-token"]
    })
    mounts = { work = "/var/lib/foxtrot-provisioner" }
    health = ["python -c \"import urllib.request as u; u.urlopen('http://127.0.0.1:8000/health', timeout=2)\""]
  }]
  task_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Resource = "*", NotAction = ["iam:*", "organizations:*", "account:*"] },
      { Effect = "Allow", Resource = "*", Action = ["iam:Get*", "iam:List*", "iam:PassRole", "iam:CreateServiceLinkedRole"] },
      { Effect = "Allow", Action = ["iam:*"], Resource = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/app-*"] },
      { Effect = "Allow", Resource = local.table_arn["foxtrot-control-plane"], Action = [
      "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Query"] },
      # Publishes quiesce evidence and the catalog into the DB adaptor's table (control area).
      { Effect = "Allow", Resource = aws_dynamodb_table.db_operator.arn, Action = [
        "dynamodb:DescribeTable", "dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem",
      "dynamodb:DeleteItem", "dynamodb:ConditionCheckItem"] },
    ]
  })
  cluster_id         = local.svc.cluster_id
  subnet_ids         = local.svc.subnet_ids
  security_group_ids = local.svc.security_group_ids
  namespace_id       = local.svc.namespace_id
  log_group          = local.svc.log_group
  region             = local.svc.region
}

# ---------------------------------------------------------------------------------------------
# Console: static platform-frontend + /api → control-plane. console.internal:80, published by
# the platform Cloudflare tunnel on the gateway.
module "console" {
  source = "../../../modules/platform_service"
  name   = "console"
  cpu    = 256
  memory = 512
  containers = [{
    name  = "web"
    image = local.image["platform-console"]
    ports = [80]
  }]
  cluster_id         = local.svc.cluster_id
  subnet_ids         = local.svc.subnet_ids
  security_group_ids = local.svc.security_group_ids
  namespace_id       = local.svc.namespace_id
  log_group          = local.svc.log_group
  region             = local.svc.region
}
