# Private CA for the platform's internal TLS (DB adaptor API and PgBouncer). Clients that call
# them (control-plane, app VMs) trust this CA.
resource "tls_private_key" "ca" {
  algorithm   = "ECDSA"
  ecdsa_curve = "P256"
}
resource "tls_self_signed_cert" "ca" {
  private_key_pem       = tls_private_key.ca.private_key_pem
  is_ca_certificate     = true
  validity_period_hours = 24 * 365
  allowed_uses          = ["cert_signing", "crl_signing"]
  subject {
    common_name  = "Foxtrot platform internal CA"
    organization = "Foxtrot"
  }
}
resource "tls_private_key" "db" {
  algorithm   = "ECDSA"
  ecdsa_curve = "P256"
}
resource "tls_cert_request" "db" {
  private_key_pem = tls_private_key.db.private_key_pem
  dns_names       = ["db-proxy.internal", "db-migration.internal", "*.internal", "localhost"]
  ip_addresses    = ["127.0.0.1"]
  subject {
    common_name = "db-proxy.internal"
  }
}
resource "tls_locally_signed_cert" "db" {
  cert_request_pem      = tls_cert_request.db.cert_request_pem
  ca_private_key_pem    = tls_private_key.ca.private_key_pem
  ca_cert_pem           = tls_self_signed_cert.ca.cert_pem
  validity_period_hours = 24 * 180
  allowed_uses          = ["digital_signature", "key_encipherment", "server_auth"]
}
locals {
  ca_pem = tls_self_signed_cert.ca.cert_pem
}
