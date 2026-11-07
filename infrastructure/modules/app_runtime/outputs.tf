output "app_env_keys" {
  description = "앱에 들어가는 환경 변수 이름 (값 제외)"
  value       = sort(keys(local.common))
}

output "environment" { value = local.common }
output "bootstrap_environment" {
  value = merge({ DB_HOST = var.database.host, DB_PORT = tostring(var.database.port),
    DB_NAME = var.database.name, APP_DB_USER = var.database.app_user },
  var.database.admin_user == null ? {} : { DB_ADMIN_USER = var.database.admin_user })
}
