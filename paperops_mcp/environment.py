"""Environment check: which keys are set, and (with probe=True) whether each database answers right now.

The point is to tell apart "needs a key you do not have", "the key was refused" and "the service is
down or slow", before blaming the tool. Key values are never read into the output.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from . import europepmc, indexes, ncbi, preprints, publishers
from .catalog import DATABASES, KEYS, key
from .core import Ctx
from .http import REASONS, SourceError

# Small, stable examples: Alexandrov et al. 2020 (open access, in PMC) and one preprint per server.
DOI, PMID, PMCID = "10.1038/s41586-020-1943-3", "32025018", "PMC7054213"
MEDRXIV_DOI, ARXIV_ID = "10.1101/2020.09.09.20191205", "2106.14599"
ELSEVIER_OA_DOI, WILEY_OA_DOI, SPRINGER_OA_DOI = "10.1016/j.cell.2020.01.001", "10.1002/humu.24309", "10.1186/s13059-020-02021-3"


def _wiley(ctx: Ctx) -> None:
    token = publishers.need_key("WILEY_TDM_TOKEN")
    ctx.get(publishers.wiley_url(WILEY_OA_DOI), headers={"Wiley-TDM-Client-Token": token}, auth=True, retries=0)


def _elsevier(ctx: Ctx) -> str:
    body = publishers.elsevier_body(publishers.elsevier_xml(ctx, ELSEVIER_OA_DOI))
    return "key accepted; open-access article body returned" if len(body) > 3000 else \
        "key accepted, but only metadata came back even for an open-access article"


def _publisher(ctx: Ctx) -> str:
    if not ctx.cfg.get("allow_publisher_direct"):
        raise SourceError("disabled", "allow_publisher_direct is false")
    return "enabled; success depends on your network's subscriptions"


PROBES: dict[str, Callable[[Ctx], Any]] = {
    "pubmed": lambda c: ncbi.pubmed_ids(c, f"{PMID}[uid]", 1),
    "europepmc": lambda c: europepmc.by_doi(c, DOI),
    "preprints": lambda c: europepmc.search_preprints(c, "mutational signature", 1, None, None, "relevance"),
    "pubtator": lambda c: ncbi.pubtator_search(c, "@GENE_BRCA1", 1, None, None, "relevance"),
    "pmc_s3": lambda c: ncbi.s3_list(c, PMCID),
    "pmc_bioc": lambda c: c.get(ncbi.bioc_url(PMCID)),
    "biorxiv": lambda c: preprints.biorxiv_details(c, MEDRXIV_DOI),
    "arxiv": lambda c: preprints.arxiv_get(c, ARXIV_ID),
    "crossref": lambda c: indexes.crossref_get(c, DOI),
    "openalex": lambda c: indexes.openalex_get(c, {"doi": DOI}),
    "semanticscholar": lambda c: indexes.s2_get(c, {"doi": DOI}),
    "unpaywall": lambda c: indexes.unpaywall_pdf_urls(c, DOI),
    "elsevier": _elsevier,
    "wiley": _wiley,
    "springer": lambda c: publishers.springer_article(c, SPRINGER_OA_DOI),
    "publisher": _publisher,
}


def _probe(ctx: Ctx, db_id: str) -> dict[str, Any]:
    start = time.monotonic()
    saved_retries = ctx.cfg.data["http"].get("retries", 2)
    ctx.cfg.data["http"]["retries"] = 0  # a probe reports what happens on the first try
    try:
        note = PROBES[db_id](ctx)
        out: dict[str, Any] = {"status": "ok", "reason": "ok"}
        if isinstance(note, str):
            out["detail"] = note
    except SourceError as exc:
        status = {"no_key": "needs_key", "no_email": "needs_email", "disabled": "off"}.get(exc.reason, "fail")
        out = {"status": status, "reason": exc.reason, "meaning": REASONS.get(exc.reason, ""),
               "detail": exc.detail[:200], "http_status": exc.status}
    except Exception as exc:
        out = {"status": "fail", "reason": "internal_error", "detail": f"{type(exc).__name__}: {exc}"[:200]}
    finally:
        ctx.cfg.data["http"]["retries"] = saved_retries
    out["seconds"] = round(time.monotonic() - start, 1)
    return out


def check_environment(ctx: Ctx, probe: bool = False, databases: list[str] | None = None) -> dict[str, Any]:
    keys = {name: {"set": bool(key(name)), "for": info["for"], "get": info["get"]} for name, info in KEYS.items()}
    out: dict[str, Any] = {"config": ctx.cfg.public_summary(), "keys": keys}
    try:
        import pypdf

        out["pdf_reader"] = f"pypdf {pypdf.__version__}"
    except ImportError:
        out["pdf_reader"] = "MISSING: pip install pypdf (PDFs cannot be validated or converted to text)"
    rows = []
    for d in DATABASES:
        if databases and d["id"] not in databases:
            continue
        row: dict[str, Any] = {"id": d["id"], "name": d["name"], "key": d["key"],
                               "keys_set": {k: bool(key(k)) for k in d["key_env"]}}
        if probe:
            row.update(_probe(ctx, d["id"]))
        elif d["key"] == "required" and not key(d["key_env"][0]):
            row.update(status="needs_key", reason="no_key")
        elif d["id"] == "unpaywall" and not ctx.cfg.email:
            row.update(status="needs_email", reason="no_email")
        elif d["id"] == "publisher" and not ctx.cfg.get("allow_publisher_direct"):
            row.update(status="off", reason="disabled")
        else:
            row["status"] = "not probed"
        rows.append(row)
    out["databases"] = rows
    if probe:
        out["summary"] = {s: [r["id"] for r in rows if r["status"] == s]
                          for s in ("ok", "needs_key", "needs_email", "off", "fail")}
        out["api_queries"] = ctx.log.calls
    else:
        out["hint"] = "call with probe=True to test every database against its live API"
    return out
