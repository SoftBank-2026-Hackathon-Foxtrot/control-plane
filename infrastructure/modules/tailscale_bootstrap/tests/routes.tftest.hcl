variables {
  name             = "foxtrot-router-test"
  cloud            = "aws"
  region           = "ap-northeast-2"
  secret_ref       = "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:test"
  advertised_cidrs = ["10.20.0.0/16"]
  remote_cidrs     = ["10.40.0.0/24", "10.60.0.0/16"]
}
run "site_to_site_bootstrap" {
  command = plan
  assert {
    condition     = strcontains(output.startup_script, "--snat-subnet-routes=false") && strcontains(output.startup_script, "--accept-routes=true") && strcontains(output.startup_script, "net.ipv4.ip_forward = 1")
    error_message = "Both sites need routed forwarding with original client addresses preserved."
  }
  assert {
    condition     = strcontains(output.startup_script, "Restart=on-failure") && strcontains(output.startup_script, "--auth-key=file:") && !strcontains(output.startup_script, "userspace-networking")
    error_message = "Use a persistent kernel router and a runtime-fetched key file."
  }
}
run "reject_overlapping_sites" {
  command = plan
  variables { remote_cidrs = ["10.20.4.0/24"] }
  expect_failures = [output.startup_script]
}
run "reject_default_route" {
  command = plan
  variables { remote_cidrs = ["0.0.0.0/0"] }
  expect_failures = [var.remote_cidrs]
}
run "reject_public_advertisement" {
  command = plan
  variables { advertised_cidrs = ["203.0.113.0/24"] }
  expect_failures = [var.advertised_cidrs]
}
