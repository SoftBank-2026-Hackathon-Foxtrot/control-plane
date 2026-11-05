"""Errors the console API returns, in the same shape as the adapters' errors."""

from __future__ import annotations


class ApiError(Exception):
    def __init__(
        self, status: int, code: str, message: str, blockers: list[str] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.blockers = blockers or []
