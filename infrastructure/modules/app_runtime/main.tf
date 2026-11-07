locals {
  # Defaults point the app at this environment's own stores. Anything in app.env wins, so a
  # migration can point DATABASE_URL / REDIS_URL / OBJECT_MIGRATION_SERVER at the platform's
  # proxies without touching this module. __X__ tokens are filled at boot from the secret store.
  defaults = merge(
    {
      DATABASE_URL       = "postgresql://${var.database.app_user}:__APP_DB_PASSWORD__@${var.database.host}:${var.database.port}/${var.database.name}?sslmode=require"
      REDIS_URL          = var.redis_url
      S3_BUCKET          = var.storage.bucket
      S3_FALLBACK_BUCKET = var.storage.bucket
      S3_REGION          = var.storage.region
      SERVED_BY          = coalesce(var.app.served_by, var.cloud)
    },
    var.storage.endpoint == null ? {} : { S3_ENDPOINT = var.storage.endpoint },
    var.storage.access_key_id == null ? {} : { AWS_ACCESS_KEY_ID = var.storage.access_key_id, AWS_SECRET_ACCESS_KEY = "__STORAGE_SECRET__" },
  )
  common = merge(local.defaults, var.app.env)
  lines  = { for k, m in { common = local.common, api = var.app.api_env, worker = var.app.worker_env } : k => join("\n", [for n in sort(keys(m)) : "${n}=${m[n]}"]) }
}
