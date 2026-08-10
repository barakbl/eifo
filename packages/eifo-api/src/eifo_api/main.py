"""ASGI entry point: ``uvicorn eifo_api.main:app``."""

from eifo_api.app import create_app

app = create_app()

__all__ = ["app"]
