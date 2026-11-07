output "service_account_email" {
  value = google_service_account.app.email
}

output "hmac_access_id" {
  value = google_storage_hmac_key.app.access_id
}
output "hmac_secret" {
  value     = google_storage_hmac_key.app.secret
  sensitive = true
}
