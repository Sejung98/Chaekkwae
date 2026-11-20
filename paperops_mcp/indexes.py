"""Cross-publisher indexes: Crossref, OpenAlex, Semantic Scholar, Unpaywall.

Checked against the live services on 2026-11-20:
- Crossref (https://api.crossref.org): /works/{doi}, /works?query.bibliographic=...  No key; `mailto` = polite pool.
- OpenAlex (https://api.openalex.org): /works?search=..., /works/doi:{doi}. Works without a key, but then every
  user behind the same IP shares one small daily budget; api_key=... gives a personal budget.
- Semantic Scholar (https://api.semanticscholar.org/graph/v1): /paper/search, /paper/DOI:{doi}. Header x-api-key.
  Without a key the search endpoint returned HTTP 429 on the first call.
- Unpaywall (https://api.unpaywall.org/v2/{doi}?email=...): a real e-mail is mandatory (HTTP 422 otherwise).
"""

from __future__ import annotations

from typing import Any

from .catalog import key
from .core import Ctx, record
from .http import SourceError
from .textio import strip_tags

CROSSREF = "https://api.crossref.org/works"
OPENALEX = "https://api.openalex.org/works"
S2 = "https://api.semanticscholar.org/graph/v1/paper"
UNPAYWALL = "https://api.unpaywall.org/v2"

_OA_SELECT = "id,doi,title,publication_year,authorships,primary_location,abstract_inverted_index,open_access,best_oa_location,locations,ids,type"
_S2_FIELDS = "title,abstract,year,venue,authors,externalIds,openAccessPdf,isOpenAccess,publicationTypes"


# ---------------------------------------------------------------- Crossref
def _crossref_record(m: dict[str, Any]) -> dict[str, Any]:
    issued = ((m.get("issued") or {}).get("date-parts") or [[None]])[0]
    rec = record(
        "crossref", doi=m.get("DOI"), title=strip_tags((m.get("title") or [""])[0]),
        authors=[" ".join(x for x in (a.get("family"), a.get("given")) if x) or a.get("name", "")
                 for a in m.get("author") or []],
        journal=(m.get("container-title") or [None])[0], year=issued[0] if issued else None,
        abstract=strip_tags(m.get("abstract")) or None, is_preprint=m.get("type") == "posted-content",
    )
    rec["links"] = [{"url": l.get("URL"), "content_type": l.get("content-type")} for l in m.get("link") or []]
    rec["publisher"] = m.get("publisher")
    return rec


def _crossref_params(ctx: Ctx, **p: Any) -> dict[str, Any]:
    if ctx.cfg.email:
        p["mailto"] = ctx.cfg.email
    return p


def crossref_get(ctx: Ctx, doi: str) -> dict[str, Any]:
    return _crossref_record(ctx.get(f"{CROSSREF}/{doi}", params=_crossref_params(ctx)).json()["message"])


def crossref_search(ctx: Ctx, query: str, max_results: int, year_from: int | None, year_to: int | None,
                    sort: str) -> tuple[list[dict], dict]:
    p = _crossref_params(ctx, **{"query.bibliographic": query, "rows": max_results})
    filters = [f"from-pub-date:{year_from}"] if year_from else []
    filters += [f"until-pub-date:{year_to}"] if year_to else []
    if filters:
        p["filter"] = ",".join(filters)
    if sort == "date":
        p.update(sort="published", order="desc")
    msg = ctx.get(CROSSREF, params=p).json()["message"]
    return [_crossref_record(m) for m in msg.get("items", [])], {"query_sent": query, "total": msg.get("total-results")}


# ---------------------------------------------------------------- OpenAlex
def _openalex_abstract(inv: dict[str, list[int]] | None) -> str | None:
    if not inv:
        return None
    words = sorted((pos, w) for w, positions in inv.items() for pos in positions)
    return " ".join(w for _, w in words)


def _openalex_record(w: dict[str, Any]) -> dict[str, Any]:
    ids = w.get("ids") or {}
    loc = w.get("primary_location") or {}
    rec = record(
        "openalex", doi=w.get("doi"), pmid=(ids.get("pmid") or "").rsplit("/", 1)[-1] or None,
        pmcid=(ids.get("pmcid") or "").rsplit("/", 1)[-1] or None, title=w.get("title"),
        authors=[(a.get("author") or {}).get("display_name", "") for a in (w.get("authorships") or [])[:50]],
        journal=(loc.get("source") or {}).get("display_name"), year=w.get("publication_year"),
        abstract=_openalex_abstract(w.get("abstract_inverted_index")),
        is_preprint=w.get("type") == "preprint", is_oa=(w.get("open_access") or {}).get("is_oa"),
    )
    urls = [(w.get("best_oa_location") or {}).get("pdf_url")]
    urls += [(l or {}).get("pdf_url") for l in w.get("locations") or [] if (l or {}).get("is_oa")]
    rec["oa_pdf_urls"] = list(dict.fromkeys(u for u in urls if u))
    return rec


def _openalex_params(**p: Any) -> tuple[dict[str, Any], bool]:
    if key("OPENALEX_API_KEY"):
        p["api_key"] = key("OPENALEX_API_KEY")
    return p, "api_key" in p


def openalex_get(ctx: Ctx, ids: dict) -> dict[str, Any]:
    ref = f"doi:{ids['doi']}" if ids.get("doi") else f"pmid:{ids['pmid']}"
    p, auth = _openalex_params(select=_OA_SELECT)
    return _openalex_record(ctx.get(f"{OPENALEX}/{ref}", params=p, auth=auth).json())


def openalex_search(ctx: Ctx, query: str, max_results: int, year_from: int | None, year_to: int | None,
                    sort: str) -> tuple[list[dict], dict]:
    p, auth = _openalex_params(search=query, select=_OA_SELECT, **{"per-page": min(max_results, 200)})
    if year_from or year_to:
        p["filter"] = f"publication_year:{year_from or 1800}-{year_to or 3000}"
    if sort == "date":
        p["sort"] = "publication_date:desc"
    data = ctx.get(OPENALEX, params=p, auth=auth).json()
    return [_openalex_record(w) for w in data.get("results", [])], \
        {"query_sent": query, "total": (data.get("meta") or {}).get("count")}


# ---------------------------------------------------------------- Semantic Scholar
def _s2_headers() -> dict[str, str] | None:
    return {"x-api-key": key("S2_API_KEY")} if key("S2_API_KEY") else None


def _s2_record(p: dict[str, Any]) -> dict[str, Any]:
    ext = p.get("externalIds") or {}
    pmc = ext.get("PubMedCentral")
    rec = record(
        "semanticscholar", doi=ext.get("DOI"), pmid=ext.get("PubMed"), pmcid=f"PMC{pmc}" if pmc else None,
        arxiv=ext.get("ArXiv"), title=p.get("title"), authors=[a.get("name", "") for a in p.get("authors") or []],
        journal=p.get("venue"), year=p.get("year"), abstract=p.get("abstract"), is_oa=p.get("isOpenAccess"),
        is_preprint="Preprint" in (p.get("publicationTypes") or []) or None,
    )
    url = (p.get("openAccessPdf") or {}).get("url")
    rec["oa_pdf_urls"] = [url] if url else []
    return rec


def _s2_hint(exc: SourceError) -> SourceError:
    if exc.reason == "rate_limited" and not key("S2_API_KEY"):
        exc.detail = "keyless requests share one global pool; set S2_API_KEY for a personal rate limit"
    return exc


def s2_get(ctx: Ctx, ids: dict) -> dict[str, Any]:
    ref = f"DOI:{ids['doi']}" if ids.get("doi") else f"PMID:{ids['pmid']}" if ids.get("pmid") else f"ARXIV:{ids['arxiv']}"
    try:
        return _s2_record(ctx.get(f"{S2}/{ref}", params={"fields": _S2_FIELDS}, headers=_s2_headers(),
                                  auth=bool(_s2_headers())).json())
    except SourceError as exc:
        raise _s2_hint(exc)


def s2_search(ctx: Ctx, query: str, max_results: int, year_from: int | None, year_to: int | None,
              sort: str) -> tuple[list[dict], dict]:
    p: dict[str, Any] = {"query": query, "limit": min(max_results, 100), "fields": _S2_FIELDS}
    if year_from or year_to:
        p["year"] = f"{year_from or ''}-{year_to or ''}"
    try:
        data = ctx.get(f"{S2}/search", params=p, headers=_s2_headers(), auth=bool(_s2_headers())).json()
    except SourceError as exc:
        raise _s2_hint(exc)
    return [_s2_record(x) for x in data.get("data") or []], {"query_sent": query, "total": data.get("total")}


# ---------------------------------------------------------------- Unpaywall
def unpaywall_pdf_urls(ctx: Ctx, doi: str) -> list[str]:
    if not ctx.cfg.email:
        raise SourceError("no_email", "Unpaywall needs a real contact e-mail: set contact_email in config.yaml "
                                      "or the PAPEROPS_EMAIL environment variable")
    try:
        data = ctx.get(f"{UNPAYWALL}/{doi}", params={"email": ctx.cfg.email}, ok_status=(200,)).json()
    except SourceError as exc:
        if exc.status == 422:
            raise SourceError("no_email", "Unpaywall rejected the contact e-mail (placeholders are refused)", 422)
        raise
    if not data.get("is_oa"):
        raise SourceError("not_open_access", "Unpaywall lists no open-access copy for this DOI")
    locs = [data.get("best_oa_location") or {}] + list(data.get("oa_locations") or [])
    urls = list(dict.fromkeys(l.get("url_for_pdf") for l in locs if l.get("url_for_pdf")))
    if not urls:
        raise SourceError("not_open_access", "open access is reported, but only as a web page (no PDF link)")
    return urls
