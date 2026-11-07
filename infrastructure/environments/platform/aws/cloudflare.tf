# Public entry for the console: a Cloudflare tunnel run by the gateway's cloudflared,
# platform.<zone> → http://console.internal:80. LB and pools stay with the traffic part.

variable "cloudflare_zone" {
  type    = string
  default = "foxtrot.live"
}

data "cloudflare_zone" "this" {
  filter = { name = var.cloudflare_zone }
}

locals {
  cf_account_id = data.cloudflare_zone.this.account.id
  console_host  = trimprefix(var.console_url, "https://")
}

resource "random_bytes" "tunnel_secret" {
  length = 32
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "platform" {
  account_id    = local.cf_account_id
  name          = "foxtrot-platform"
  config_src    = "cloudflare"
  tunnel_secret = random_bytes.tunnel_secret.base64
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "platform" {
  account_id = local.cf_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.platform.id
  config = {
    ingress = [
      { hostname = local.console_host, service = "http://console.internal:80" },
      { service = "http_status:404" },
    ]
  }
}

resource "cloudflare_dns_record" "console" {
  zone_id = data.cloudflare_zone.this.zone_id
  name    = local.console_host
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.platform.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
  comment = "Foxtrot platform console (tunnel foxtrot-platform, managed by Terraform)"
}

data "cloudflare_zero_trust_tunnel_cloudflared_token" "platform" {
  account_id = local.cf_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.platform.id
}

# The tunnel token secret used to be a hand-filled slot; keep the same secret.
moved {
  from = aws_secretsmanager_secret.external["cloudflare-tunnel-platform"]
  to   = aws_secretsmanager_secret.tunnel_platform
}
moved {
  from = aws_secretsmanager_secret_version.external["cloudflare-tunnel-platform"]
  to   = aws_secretsmanager_secret_version.tunnel_platform
}
