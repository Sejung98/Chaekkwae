"""Loading config.yaml. Everything has a default, so the tools run with no config file at all.

Config file lookup order:
1. Environment variable PAPEROPS_CONFIG
2. <repository root>/config.yaml

Relative paths are resolved against the config file's directory (or the repository root).
API keys are never stored in the config file: they are read from environment variables only
(names are listed in catalog.KEYS).
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, Any] = {
    "data_root": "./data",          # downloaded papers and saved search results
    "state_dir": "./state",         # SQLite log of searches and fetch attempts
    "contact_email": "",            # sent to NCBI / Crossref / Unpaywall as the polite-contact address
    "allow_publisher_direct": False,  # last rung: follow the DOI to the publisher site (needs a subscription by IP)
    "max_download_mb": 200,
    "max_supplement_files": 50,
    "http": {"timeout": 60, "retries": 2},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    def __init__(self, data: dict[str, Any], source: Path | None, base: Path):
        self.data = data
        self.source = source
        self.base = base

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def path(self, key: str) -> Path:
        p = Path(os.path.expanduser(os.path.expandvars(str(self.data[key]))))
        return (p if p.is_absolute() else self.base / p).resolve()

    @property
    def data_root(self) -> Path:
        return self.path("data_root")

    @property
    def db_path(self) -> Path:
        return self.path("state_dir") / "paperops.sqlite"

    @property
    def email(self) -> str:
        return os.environ.get("PAPEROPS_EMAIL") or str(self.data.get("contact_email") or "")

    @property
    def timeout(self) -> float:
        return float(self.data["http"].get("timeout", 60))

    @property
    def retries(self) -> int:
        return int(self.data["http"].get("retries", 2))

    def public_summary(self) -> dict[str, Any]:
        """Summary for tool output. The contact e-mail is reported only as set / not set."""
        return {
            "config_file": str(self.source) if self.source else None,
            "data_root": str(self.data_root),
            "state_db": str(self.db_path),
            "contact_email": "set" if self.email else "not set",
            "allow_publisher_direct": bool(self.data.get("allow_publisher_direct")),
            "max_download_mb": self.data.get("max_download_mb"),
            "http": self.data.get("http"),
        }


_CACHE: dict[str, Config] = {}


def load_config(path: str | os.PathLike | None = None, *, use_cache: bool = True) -> Config:
    if path is None:
        path = os.environ.get("PAPEROPS_CONFIG") or (REPO_ROOT / "config.yaml")
    p = Path(path).expanduser()
    key = str(p.resolve()) if p.exists() else str(p)
    if use_cache and key in _CACHE:
        return _CACHE[key]
    user: dict[str, Any] = {}
    source: Path | None = None
    if p.exists():
        with open(p, encoding="utf-8") as fh:
            user = yaml.safe_load(fh) or {}
        source = p.resolve()
    cfg = Config(_deep_merge(DEFAULTS, user), source, source.parent if source else REPO_ROOT)
    _CACHE[key] = cfg
    return cfg


def clear_cache() -> None:
    _CACHE.clear()
