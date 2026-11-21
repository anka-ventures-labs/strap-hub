"""Shared SQLite store. Schema is defined by docs/CONTRACT.md section 4."""
from .db import SCHEMA_VERSION, connect

__all__ = ["SCHEMA_VERSION", "connect"]
