"""Preprint servers: bioRxiv / medRxiv and arXiv.

Checked against the live services on 2026-11-20:
- bioRxiv API (https://api.biorxiv.org/): GET /details/{biorxiv|medrxiv}/{doi}/na/json
  -> collection[] with one entry per version: title, authors, date, version, abstract, jatsxml, published.
  There is no keyword search; that goes through Europe PMC (source `preprints`).
  PDF: https://www.{server}.org/content/{doi}v{version}.full.pdf
- arXiv API (https://info.arxiv.org/help/api/): GET https://export.arxiv.org/api/query
  ?search_query=...&max_results=N (Atom XML) or ?id_list=ID. PDF: https://arxiv.org/pdf/{id}
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

from .core import Ctx, record
from .http import SourceError
from .textio import squash

BIORXIV_API = "https://api.biorxiv.org/details"
ARXIV_API = "https://export.arxiv.org/api/query"
_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV_NS = "{http://arxiv.org/schemas/atom}"


def biorxiv_details(ctx: Ctx, doi: str) -> dict[str, Any]:
    """Latest version of a bioRxiv/medRxiv preprint, plus its PDF and JATS URLs."""
    for server in ("biorxiv", "medrxiv"):
        coll = ctx.get(f"{BIORXIV_API}/{server}/{doi}/na/json").json().get("collection") or []
        if coll:
            v = coll[-1]
            rec = record(
                "biorxiv", doi=v.get("doi") or doi, title=v.get("title"),
                authors=[a.strip() for a in (v.get("authors") or "").split(";") if a.strip()],
                journal=server, year=(v.get("date") or "")[:4], abstract=v.get("abstract"), is_preprint=True,
                is_oa=True,
            )
            published = v.get("published")
            rec.update(server=server, version=v.get("version"), license=v.get("license"),
                       published_doi=published if published and published != "NA" else None,
                       jats_url=v.get("jatsxml"),
                       pdf_url=f"https://www.{server}.org/content/{doi}v{v.get('version', 1)}.full.pdf")
            return rec
    raise SourceError("not_found", f"{doi} is not a bioRxiv or medRxiv preprint")


def _arxiv_entries(xml: bytes) -> list[dict[str, Any]]:
    out = []
    for e in ET.fromstring(xml).iter(f"{_ATOM}entry"):
        m = re.search(r"arxiv\.org/abs/(.+?)(v\d+)?$", e.findtext(f"{_ATOM}id") or "")
        if not m:
            continue
        aid = m.group(1)
        out.append(record(
            "arxiv", arxiv=aid, doi=e.findtext(f"{_ARXIV_NS}doi") or f"10.48550/arxiv.{aid}",
            title=squash(e.findtext(f"{_ATOM}title") or ""), abstract=squash(e.findtext(f"{_ATOM}summary") or ""),
            authors=[a.findtext(f"{_ATOM}name") or "" for a in e.iter(f"{_ATOM}author")],
            journal="arXiv", year=(e.findtext(f"{_ATOM}published") or "")[:4], is_preprint=True, is_oa=True,
        ))
    return out


def arxiv_search(ctx: Ctx, query: str, max_results: int, year_from: int | None, year_to: int | None,
                 sort: str) -> tuple[list[dict], dict]:
    if re.search(r"\b(all|ti|abs|au|cat):", query):
        q = query                                   # already in arXiv syntax
    elif re.search(r'"|\b(AND|OR|ANDNOT)\b', query):
        q = f"all:{query}"
    else:                                           # plain words: require all of them (arXiv's default is OR)
        q = " AND ".join(f"all:{w}" for w in query.split())
    if year_from or year_to:
        q = f"({q}) AND submittedDate:[{year_from or 1991}01010000 TO {year_to or 2100}12312359]"
    params = {"search_query": q, "start": 0, "max_results": max_results,
              "sortBy": "submittedDate" if sort == "date" else "relevance"}
    resp = ctx.get(ARXIV_API, params=params)
    total = re.search(r"<opensearch:totalResults[^>]*>(\d+)<", resp.text)
    return _arxiv_entries(resp.content), {"query_sent": q, "total": int(total.group(1)) if total else None}


def arxiv_get(ctx: Ctx, arxiv_id: str) -> dict[str, Any]:
    entries = _arxiv_entries(ctx.get(ARXIV_API, params={"id_list": arxiv_id}).content)
    if not entries or not entries[0].get("title"):
        raise SourceError("not_found", f"arXiv has no paper {arxiv_id}")
    return entries[0]


def arxiv_pdf_url(arxiv_id: str) -> str:
    return f"https://arxiv.org/pdf/{arxiv_id}"
