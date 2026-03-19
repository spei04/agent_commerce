"""
Vercel Serverless entrypoint.

Vercel's Python runtime routes requests under /api/* to functions in this folder.
This module exposes the FastAPI ASGI app as `app`.
"""

from main import app  # noqa: F401

