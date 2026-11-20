"""The context every source function receives: config, state DB, HTTP client and the API call log."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from . import http
from .config import Config, load_config
from .state import StateDB


@dataclass
class Ctx:
    cfg: Config
    db: StateDB
    client: httpx.Client
    log: http.ApiLog = field(default_factory=http.ApiLog)

    def get(self, url: str, **kw: Any) -> httpx.Response:
        kw.setdefault("retries", self.cfg.retries)
        return http.request(self.client, self.log, "GET", url, **kw)

    def save(self, url: str, dest: Path, **kw: Any) -> dict[str, Any]:
        kw.setdefault("max_mb", float(self.cfg.get("max_download_mb", 200)))
        return http.download(self.client, self.log, url, dest, **kw)

    def __enter__(self) -> "Ctx":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.client.close()


def open_ctx(cfg: Config | None = None) -> Ctx:
    cfg = cfg or load_config()
    return Ctx(cfg, StateDB(cfg.db_path), http.make_client(cfg.timeout, cfg.email))


def record(source: str, **f: Any) -> dict[str, Any]:
    """One paper in the common shape used by every source."""
    out = {"pmid": None, "pmcid": None, "doi": None, "arxiv": None, "title": None, "authors": [],
           "journal": None, "year": None, "abstract": None, "is_preprint": False, "is_oa": None,
           "source": source}
    out.update({k: v for k, v in f.items() if v not in (None, "", [])})
    if out["doi"]:
        out["doi"] = str(out["doi"]).lower().removeprefix("https://doi.org/")
    if out["pmid"]:
        out["pmid"] = str(out["pmid"])
    if out["year"]:
        try:
            out["year"] = int(str(out["year"])[:4])
        except ValueError:
            out["year"] = None
    return out
