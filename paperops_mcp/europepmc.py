"""Europe PMC: search (articles and preprints), full-text XML and supplementary files of the OA subset.

Checked against the live service on 2026-11-20 (https://europepmc.org/RestfulWebService):
- GET /search?query=...&format=json&resultType=core&pageSize=N
- GET /{PMCID}/fullTextXML           (404 when the article is not in the open-access subset)
- GET /{PMCID}/supplementaryFiles    (zip; 404 when there are none)
Responses took 25-47 s on that day, so timeouts here are expected and reported as `timeout`.
"""

from __future__ import annotations

from typing import Any

from .core import Ctx, record

EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"


def _record(r: dict[str, Any], source: str) -> dict[str, Any]:
    journal = ((r.get("journalInfo") or {}).get("journal") or {}).get("title") \
        or (r.get("bookOrReportDetails") or {}).get("publisher")
    return record(
        source, pmid=r.get("pmid"), pmcid=r.get("pmcid"), doi=r.get("doi"), title=r.get("title"),
        authors=[a.strip() for a in (r.get("authorString") or "").rstrip(".").split(",") if a.strip()],
        journal=journal, year=r.get("pubYear"), abstract=r.get("abstractText"),
        is_preprint=r.get("source") == "PPR", is_oa=(r.get("isOpenAccess") == "Y") if r.get("isOpenAccess") else None,
        cited_by=r.get("citedByCount"),
    )


def _search(ctx: Ctx, source: str, q: str, max_results: int, sort: str) -> tuple[list[dict], dict]:
    params = {"query": q, "format": "json", "resultType": "core", "pageSize": min(max_results, 100)}
    if sort == "date":
        params["sort"] = "P_PDATE_D desc"
    data = ctx.get(f"{EPMC}/search", params=params).json()
    recs = [_record(r, source) for r in (data.get("resultList") or {}).get("result", [])]
    return recs, {"query_sent": q, "total": data.get("hitCount")}


def _with_years(q: str, year_from: int | None, year_to: int | None) -> str:
    if year_from or year_to:
        q = f"({q}) AND PUB_YEAR:[{year_from or 1800} TO {year_to or 3000}]"
    return q


def search(ctx: Ctx, query: str, max_results: int, year_from: int | None, year_to: int | None,
           sort: str) -> tuple[list[dict], dict]:
    return _search(ctx, "europepmc", _with_years(query, year_from, year_to), max_results, sort)


def search_preprints(ctx: Ctx, query: str, max_results: int, year_from: int | None, year_to: int | None,
                     sort: str) -> tuple[list[dict], dict]:
    return _search(ctx, "preprints", _with_years(f"({query}) AND SRC:PPR", year_from, year_to), max_results, sort)


def by_doi(ctx: Ctx, doi: str) -> dict[str, Any] | None:
    recs, _ = _search(ctx, "europepmc", f'DOI:"{doi}"', 1, "relevance")
    return recs[0] if recs else None


def fulltext_url(pmcid: str) -> str:
    return f"{EPMC}/{pmcid}/fullTextXML"


def supplement_url(pmcid: str) -> str:
    return f"{EPMC}/{pmcid}/supplementaryFiles"
