"""Shared envelope helpers for plugin responses."""


def success_envelope(result: dict) -> dict:
    return {"result": result}


def error_envelope(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}
