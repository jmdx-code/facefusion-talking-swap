"""Centralized resolution for portable local user data & credentials (`userdata/`)."""
from __future__ import annotations

import os
from pathlib import Path


def get_app_root() -> Path:
    return Path(__file__).resolve().parent.parent


def get_package_root() -> Path:
    app_root = get_app_root()
    if app_root.parent.name.lower() == 'app':
        return app_root.parent.parent
    return app_root


def get_userdata_dir() -> Path:
    override = os.environ.get('FACEFUSION_USERDATA_DIR', '').strip()
    if override:
        return Path(override).resolve()
    return get_package_root() / 'userdata'
