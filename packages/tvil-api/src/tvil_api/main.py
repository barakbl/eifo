"""ASGI entry point: ``uvicorn tvil_api.main:app``."""

from tvil_api.app import create_app

app = create_app()

__all__ = ["app"]
