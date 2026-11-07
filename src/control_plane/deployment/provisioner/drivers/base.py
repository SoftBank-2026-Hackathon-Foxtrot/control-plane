"""Small execution boundary shared by Terraform and Ansible."""
from typing import Protocol


class DriverError(RuntimeError):
    def __init__(self, code, message, *, uncertain=False):
        super().__init__(message)
        self.code, self.uncertain = code, uncertain


class ProvisioningDriver(Protocol):
    def plan(self, environment: dict, spec: dict, context: dict) -> dict: ...
    def apply(self, environment: dict, plan: dict, context: dict) -> dict: ...
    def inspect(self, environment: dict, context: dict) -> dict: ...
    def cleanup(self, environment: dict, plan: dict, context: dict) -> dict: ...
