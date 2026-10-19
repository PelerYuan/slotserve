from typing import Any

from fastapi import HTTPException


class ApiError(HTTPException):
    """HTTP error whose detail is a machine-readable code (translated by the frontend)."""

    def __init__(self, status_code: int, code: str, **params: Any):
        super().__init__(status_code, detail={"code": code, "params": params})
