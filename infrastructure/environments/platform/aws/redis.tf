# Redis proxy backends. The proxy, statemig and the sync worker read STATEMIG_ENDPOINTS
# (name=url,...). The app environments' Redis have no AUTH (the proxy speaks plain RESP), so
# the URLs carry no password; the same list serves the proxy and the admin side.
locals {
  redis_endpoints_value = join(",", [for name in sort(keys(var.redis_endpoints)) : "${name}=${var.redis_endpoints[name]}"])
}
resource "aws_secretsmanager_secret" "redis_endpoints" {
  for_each                = toset(["redis-endpoints-proxy", "redis-endpoints-admin"])
  name                    = "foxtrot/platform/${each.key}"
  description             = "STATEMIG_ENDPOINTS (name=redis://host:port,...); written by Terraform from var.redis_endpoints"
  recovery_window_in_days = 0
}
resource "aws_secretsmanager_secret_version" "redis_endpoints" {
  for_each      = aws_secretsmanager_secret.redis_endpoints
  secret_id     = each.value.id
  secret_string = local.redis_endpoints_value == "" ? "UNSET" : local.redis_endpoints_value
}
moved {
  from = aws_secretsmanager_secret.external["redis-endpoints-proxy"]
  to   = aws_secretsmanager_secret.redis_endpoints["redis-endpoints-proxy"]
}
moved {
  from = aws_secretsmanager_secret.external["redis-endpoints-admin"]
  to   = aws_secretsmanager_secret.redis_endpoints["redis-endpoints-admin"]
}
moved {
  from = aws_secretsmanager_secret_version.external["redis-endpoints-proxy"]
  to   = aws_secretsmanager_secret_version.redis_endpoints["redis-endpoints-proxy"]
}
moved {
  from = aws_secretsmanager_secret_version.external["redis-endpoints-admin"]
  to   = aws_secretsmanager_secret_version.redis_endpoints["redis-endpoints-admin"]
}
