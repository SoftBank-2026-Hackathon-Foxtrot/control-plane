# Optional security dashboard wiring. No workflow, seed, app IAM or data changes.
variable "security_review" {
  description = "Security comparison configuration and existing credential secret ARNs. Pass the environment-specific tfvars on every platform deployment."
  type = object({
    config_json       = string
    aws_role_arn      = string
    reader_secret_arn = string
    writer_secret_arn = optional(string)
  })
  default = null
  validation {
    condition = var.security_review == null ? true : try(
      jsondecode(var.security_review.config_json).readOnly || var.security_review.writer_secret_arn != null,
      false
    )
    error_message = "Set readOnly explicitly; writable reviews require a separate writer secret ARN."
  }
}

locals {
  security_review_environment = var.security_review == null ? {} : {
    CONTROL_PLANE_SECURITY_REVIEW_CONFIG_JSON = var.security_review.config_json
  }
  security_review_secrets = var.security_review == null ? {} : merge(
    { SECURITY_GCP_CREDENTIALS = var.security_review.reader_secret_arn },
    var.security_review.writer_secret_arn == null ? {} : {
      SECURITY_GCP_APPLY_CREDENTIALS = var.security_review.writer_secret_arn
    }
  )
}

resource "aws_iam_role_policy" "security_review_read" {
  count = var.security_review == null ? 0 : 1
  name  = "security-review-read-only"
  role  = "foxtrot-control-plane-task"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "iam:GetRolePolicy"
      Resource = var.security_review.aws_role_arn
    }]
  })
  depends_on = [module.control_plane]
}
