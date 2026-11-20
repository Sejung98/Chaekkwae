"""Keyword search across several databases, merged into one de-duplicated list.

What is logged for every search (SQLite `queries`, and returned as `sources`):
the exact query string sent to each database, how PubMed interpreted it, how many hits each
database reported, and for a database that failed, the reason.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from . import europepmc, indexes, ncbi, preprints
from .core import Ctx
from .http import SourceError
from .ids import norm_title
from .state import new_query_id

SearchFn = Callable[[Ctx, str, int, "int | None", "int | None", str], "tuple[list[dict], dict]"]
SOURCES: dict[str, SearchFn] = {
    "pubmed": ncbi.pubmed_search,
    "europepmc": europepmc.search,
    "preprints": europepmc.search_preprints,
    "pubtator": ncbi.pubtator_search,
    "arxiv": preprints.arxiv_search,
    "crossref": indexes.crossref_search,
    "openalex": indexes.openalex_search,
    "semanticscholar": indexes.s2_search,
}
DEFAULT_SOURCES = ["pubmed", "europepmc"]
_FILL = ("pmid", "pmcid", "doi", "arxiv", "title", "journal", "year", "abstract", "is_oa", "mesh", "pub_types")


def merge(per_source: dict[str, list[dict]]) -> list[dict[str, Any]]:
    """De-duplicate by DOI, then PMID, then (only for records with neither) title.
    Order by reciprocal-rank fusion, so papers ranked high by several databases come first."""
    merged: list[dict[str, Any]] = []
    index: dict[str, int] = {}
    for source, recs in per_source.items():
        for rank, rec in enumerate(recs):
            keys = [f"doi:{rec['doi']}"] if rec.get("doi") else []
            keys += [f"pmid:{rec['pmid']}"] if rec.get("pmid") else []
            if not keys and norm_title(rec.get("title")):
                keys = [f"title:{norm_title(rec['title'])}"]
            if not keys:
                continue
            at = next((index[k] for k in keys if k in index), None)
            if at is None:
                at = len(merged)
                merged.append({**rec, "found_in": [], "score": 0.0})
                merged[at].pop("source", None)
            m = merged[at]
            for k in _FILL:
                if m.get(k) in (None, "", []) and rec.get(k) not in (None, "", []):
                    m[k] = rec[k]
            if not m.get("authors"):
                m["authors"] = rec.get("authors") or []
            m["is_preprint"] = bool(m.get("is_preprint") or rec.get("is_preprint"))
            if source not in m["found_in"]:
                m["found_in"].append(source)
            m["score"] += 1.0 / (60 + rank)
            for k in keys:
                index[k] = at
    merged.sort(key=lambda m: -m["score"])
    return merged


def _compact(m: dict[str, Any]) -> dict[str, Any]:
    authors = m.get("authors") or []
    abstract = m.get("abstract") or ""
    return {
        "pmid": m.get("pmid"), "pmcid": m.get("pmcid"), "doi": m.get("doi"), "arxiv": m.get("arxiv"),
        "title": m.get("title"), "authors": ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else ""),
        "journal": m.get("journal"), "year": m.get("year"), "is_preprint": m.get("is_preprint"),
        "is_oa": m.get("is_oa"), "found_in": m["found_in"],
        "abstract": abstract[:500] + ("…" if len(abstract) > 500 else "") or None,
    }


def search_papers(ctx: Ctx, query: str, sources: list[str] | None = None, max_results: int = 20,
                  year_from: int | None = None, year_to: int | None = None, sort: str = "relevance",
                  source_queries: dict[str, str] | None = None) -> dict[str, Any]:
    sources = list(sources or DEFAULT_SOURCES)
    unknown = sorted(set(sources) - set(SOURCES))
    if unknown:
        raise ValueError(f"unknown sources {unknown}; valid: {sorted(SOURCES)}")
    if sort not in ("relevance", "date"):
        raise ValueError("sort must be 'relevance' or 'date'")
    max_results = max(1, min(int(max_results), 100))
    status: dict[str, dict[str, Any]] = {}
    found: dict[str, list[dict]] = {}
    for name in sources:
        q = (source_queries or {}).get(name) or query
        try:
            recs, info = SOURCES[name](ctx, q, max_results, year_from, year_to, sort)
            found[name] = recs
            status[name] = {"status": "ok", "n_returned": len(recs), **info}
        except SourceError as exc:
            status[name] = {"status": "fail", "reason": exc.reason, "detail": exc.detail[:300], "query_sent": q}
    merged = merge(found)
    qid = new_query_id("search")
    out_dir = ctx.cfg.data_root / "searches"
    out_dir.mkdir(parents=True, exist_ok=True)
    result_path = out_dir / f"{qid}.json"
    params = {"query": query, "sources": sources, "max_results": max_results, "year_from": year_from,
              "year_to": year_to, "sort": sort, "source_queries": source_queries}
    result_path.write_text(json.dumps({"query_id": qid, "params": params, "sources": status, "results": merged},
                                      ensure_ascii=False, indent=1), encoding="utf-8")
    summary = {"n_unique": len(merged), "sources": status}
    ctx.db.save_query("search", params, summary, ctx.log.calls, str(result_path), query_id=qid)
    out: dict[str, Any] = {
        "ok": any(s["status"] == "ok" for s in status.values()), "query_id": qid, "query": query,
        "sources": status, "n_unique": len(merged), "results": [_compact(m) for m in merged[:max_results]],
        "result_path": str(result_path), "api_queries": ctx.log.calls,
    }
    failed = [n for n, s in status.items() if s["status"] == "fail"]
    if failed:
        out["warning"] = f"{', '.join(failed)} did not answer (see sources.<name>.reason); results are partial."
    return out
