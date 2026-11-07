terraform {
  required_version = ">= 1.10.0, < 2.0.0"
}

variable "name" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,40}$", var.name))
    error_message = "Use a lowercase hostname, 3 through 41 characters."
  }
}

variable "cloud" {
  type = string
  validation {
    condition     = contains(["aws", "gcp"], var.cloud)
    error_message = "Use aws or gcp."
  }
}

variable "secret_ref" {
  type = string
}

variable "region" {
  type = string
}

variable "advertised_cidrs" {
  type = set(string)
  validation {
    condition = length(var.advertised_cidrs) > 0 && alltrue([
      for c in var.advertised_cidrs : try(
        can(cidrnetmask(c)) &&
        alltrue([for ip in [cidrhost(c, 0), cidrhost(c, -1)] :
      can(regex("^(10\\.|192\\.168\\.|172\\.(1[6-9]|2[0-9]|3[01])\\.)", ip))]), false)
    ])
    error_message = "Advertise explicit RFC1918 IPv4 subnets only; no default or public routes."
  }
}

variable "remote_cidrs" {
  type = set(string)
  validation {
    condition = length(var.remote_cidrs) > 0 && alltrue([
      for c in var.remote_cidrs : try(
        can(cidrnetmask(c)) &&
        alltrue([for ip in [cidrhost(c, 0), cidrhost(c, -1)] :
      can(regex("^(10\\.|192\\.168\\.|172\\.(1[6-9]|2[0-9]|3[01])\\.)", ip))]), false)
    ])
    error_message = "Route explicit remote RFC1918 IPv4 subnets only."
  }
}

variable "tailscale_version" {
  type    = string
  default = "1.104.4"
  validation {
    condition     = can(regex("^[0-9]+\\.[0-9]+\\.[0-9]+$", var.tailscale_version))
    error_message = "Pin an exact stable Tailscale package version."
  }
}

locals {
  routes_disjoint = alltrue(flatten([
    for a in var.advertised_cidrs : [for b in var.remote_cidrs :
      cidrhost("${cidrhost(a, 0)}/${min(tonumber(split("/", a)[1]), tonumber(split("/", b)[1]))}", 0) !=
      cidrhost("${cidrhost(b, 0)}/${min(tonumber(split("/", a)[1]), tonumber(split("/", b)[1]))}", 0)
    ]
  ]))
  config = base64encode(jsonencode({
    name             = var.name
    cloud            = var.cloud
    secret_ref       = var.secret_ref
    region           = var.region
    advertised_cidrs = sort(tolist(var.advertised_cidrs))
  }))
}

output "startup_script" {
  value = templatefile("${path.module}/router.sh.tftpl", {
    config = local.config, version = var.tailscale_version, cloud = var.cloud
  })
  precondition {
    condition     = local.routes_disjoint
    error_message = "Local and remote CIDRs overlap. Site-to-site routing requires disjoint networks."
  }
}
