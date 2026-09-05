"""Immutable build constants.

This module holds ONLY constants that never change at runtime.
It deliberately does **not** know where ComfyUI is installed: that question is
answered exclusively by ``app.core.config_service`` (fixes B6).
"""

from __future__ import annotations

from pathlib import Path

APP_NAME = "Geekatplay ComfyUI Asset Vault"
AUTHOR = "Geekatplay Studio - Vladimir Chopine"
VERSION = "2.3.0"

API_PREFIX = "/api/v1"
DEFAULT_PORT = 8127

BACKEND_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BACKEND_DIR / "data"
DB_PATH = DATA_DIR / "vault.db"
LEGACY_DB_PATH = DATA_DIR / "asset_vault.db"
THUMB_DIR = DATA_DIR / "thumbs"
EMBED_MODEL_DIR = DATA_DIR / "models" / "all-MiniLM-L6-v2"

SCHEMA_VERSION = 8

# Parser versions.  Bumping one forces a targeted re-parse of that kind only.
PARSER_VERSION_MODEL = 4
PARSER_VERSION_NODE = 5
PARSER_VERSION_WORKFLOW = 4
PARSER_VERSION_OUTPUT = 4

TRASH_DIRNAME = ".vault-trash"
#: Where a download that failed verification is parked (C9 / SECURITY_REVIEW R4).
QUARANTINE_DIRNAME = ".vault-quarantine"

DATA_DIR.mkdir(parents=True, exist_ok=True)


class Settings:
    """Immutable build constants exposed as an object for convenience.

    NOTE: there is intentionally no ``COMFYUI_PATH`` here.  See B6.
    """

    APP_NAME = APP_NAME
    AUTHOR = AUTHOR
    VERSION = VERSION
    API_PREFIX = API_PREFIX
    DATA_DIR = DATA_DIR
    DB_PATH = DB_PATH
    DEFAULT_PORT = DEFAULT_PORT


settings = Settings()
