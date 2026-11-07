"""Adapt the existing saved-plan runner without changing v1 inputs or outputs."""
from .base import DriverError


class TerraformDriver:
    name = "terraform"

    def __init__(self, terraform):
        self.terraform = terraform

    def plan(self, environment, spec, context):
        return self.terraform.plan(environment["id"], environment["cloud"], spec,
                                   destroy=context.get("intent") == "cleanup")

    def apply(self, environment, plan, context):
        return self.terraform.apply(environment["id"], environment["cloud"], plan["plan"])

    def cleanup(self, environment, plan, context):
        self.apply(environment, plan, context)
        return {"status": "DESTROYED"}

    def inspect(self, environment, context):
        # A saved output is not evidence of current health. No implicit refresh/apply.
        raise DriverError("INSPECTION_UNAVAILABLE", "Inspect Terraform state and resources before reconciliation")
