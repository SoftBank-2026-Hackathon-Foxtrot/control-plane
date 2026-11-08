mock_provider "aws" {}
mock_provider "google" {}
mock_provider "tailscale" {}

variables {
  account_id = "123456789012"
  region     = "ap-northeast-2"
  project_id = "test-project"
  prefix     = "test"
  api_key    = "test-only-api-key"
  routes = {
    "10.30.0.0/16" = ["tag:mgmt"]
    "10.40.0.0/16" = ["tag:aws"]
    "10.50.0.0/24" = ["tag:gcp"]
    "10.51.0.0/16" = ["tag:gcp"]
  }
}

run "management_joins_the_application_tailnet" {
  command = plan
  assert {
    condition     = contains(tailscale_tailnet_key.router["mgmt"].tags, "tag:mgmt") && tailscale_tailnet_key.router["mgmt"].preauthorized && tailscale_tailnet_key.router["mgmt"].reusable
    error_message = "The platform router needs its own preauthorized role in the application tailnet."
  }
  assert {
    condition     = contains(jsondecode(tailscale_acl.this.acl).autoApprovers.routes["10.30.0.0/16"], "tag:mgmt") && contains(jsondecode(tailscale_acl.this.acl).acls[0].src, "10.30.0.0/16") && contains(jsondecode(tailscale_acl.this.acl).acls[0].dst, "10.30.0.0/16:*")
    error_message = "Platform tasks need both management route approval and bidirectional subnet access."
  }
  assert {
    condition     = contains(keys(jsondecode(tailscale_acl.this.acl).tagOwners), "tag:mgmt") && length(tailscale_tailnet_key.router) == 3
    error_message = "Management, AWS and GCP must use roles owned by the same tailnet policy."
  }
  assert {
    condition = (
      contains(keys(jsondecode(tailscale_acl.this.acl).tagOwners), "tag:onprem") &&
      !contains(jsondecode(tailscale_acl.this.acl).acls[0].src, "tag:onprem") &&
      !contains(jsondecode(tailscale_acl.this.acl).acls[0].dst, "tag:onprem:*") &&
      anytrue([for rule in jsondecode(tailscale_acl.this.acl).acls : contains(rule.dst, "100.115.99.73:5432") && try(rule.proto, "") == "tcp"])
    )
    error_message = "The team VM must get its DB rule without gaining router-wide access."
  }
}

run "other_tailnet_keeps_cloud_only_policy" {
  command = plan
  variables {
    tailscale_tailnet = "other.example.com"
  }
  assert {
    condition     = length(jsondecode(tailscale_acl.this.acl).acls) == 2 && !contains(keys(jsondecode(tailscale_acl.this.acl).tagOwners), "tag:onprem")
    error_message = "Do not install Foxtrot demo endpoints in another tailnet."
  }
}
