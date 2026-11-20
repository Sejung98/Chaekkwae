"""Resolving an identifier to a paper: all its IDs, bibliographic data and the abstract.

Order: PubMed first (authoritative for biomedical papers), then the preprint server for preprint DOIs,
then Crossref -> OpenAlex -> Europe PMC -> Semantic Scholar until a title and an abstract are found.
Every step is listed in `lookup`, so it is visible which database answered and which failed.
"""

from __future__ import annotations

from typing import Any, Callable

from . import europepmc, ids, indexes, ncbi, preprints
from .core import Ctx
from .http import SourceError

_FILL = ("pmid", "pmcid", "doi", "arxiv", "title", "authors", "journal", "year", "mesh", "pub_types", "is_oa",
         "published_doi", "server", "version")


def _merge(paper: dict[str, Any], rec: dict[str, Any] | None) -> None:
    if not rec:
        return
    for k in _FILL:
        if paper.get(k) in (None, "", []) and rec.get(k) not in (None, "", []):
            paper[k] = rec[k]
    if not paper.get("abstract") and rec.get("abstract"):
        paper["abstract"], paper["abstract_source"] = rec["abstract"], rec["source"]
    paper["is_preprint"] = bool(paper.get("is_preprint") or rec.get("is_preprint"))


def _step(paper: dict[str, Any], name: str, fn: Callable[[], dict | None]) -> dict | None:
    try:
        rec = fn()
    except SourceError as exc:
        paper["lookup"].append({"source": name, "status": "fail", "reason": exc.reason, "detail": exc.detail[:200]})
        return None
    paper["lookup"].append({"source": name, "status": "ok" if rec else "empty"})
    return rec


def resolve(ctx: Ctx, identifier: str, need_abstract: bool = True) -> dict[str, Any]:
    paper: dict[str, Any] = {"input": identifier, "pmid": None, "pmcid": None, "doi": None, "arxiv": None,
                             "title": None, "authors": [], "journal": None, "year": None, "abstract": None,
                             "abstract_source": None, "is_preprint": False, "lookup": []}
    paper.update(ids.parse(identifier))

    if paper["pmcid"] and not paper["pmid"]:
        _merge(paper, _step(paper, "pmc_idconv", lambda: ncbi.idconv(ctx, paper["pmcid"])))
    if paper["arxiv"]:
        _merge(paper, _step(paper, "arxiv", lambda: preprints.arxiv_get(ctx, paper["arxiv"])))
        paper["doi"] = paper["doi"] or f"10.48550/arxiv.{paper['arxiv']}"
        paper["is_preprint"] = True
    elif paper["doi"] and not paper["pmid"]:
        _merge(paper, _step(paper, "pubmed_doi_lookup", lambda: (
            {"pmid": p} if (p := ncbi.pmid_for_doi(ctx, paper["doi"])) else None)))

    in_pubmed = False
    if paper["pmid"]:
        rec = _step(paper, "pubmed", lambda: (ncbi.pubmed_fetch(ctx, [paper["pmid"]]) or [None])[0])
        in_pubmed = bool(rec)
        _merge(paper, rec)
    if ids.is_preprint_doi(paper["doi"]) and not paper["abstract"]:
        _merge(paper, _step(paper, "biorxiv", lambda: preprints.biorxiv_details(ctx, paper["doi"])))

    # PubMed is trusted when it says there is no abstract (editorials, letters); only look further without it.
    if paper["doi"] and not in_pubmed:
        id_view = {k: paper[k] for k in ("doi", "pmid", "arxiv")}
        for name, fn in (("crossref", lambda: indexes.crossref_get(ctx, paper["doi"])),
                         ("openalex", lambda: indexes.openalex_get(ctx, id_view)),
                         ("europepmc", lambda: europepmc.by_doi(ctx, paper["doi"])),
                         ("semanticscholar", lambda: indexes.s2_get(ctx, id_view))):
            if paper["title"] and (paper["abstract"] or not need_abstract):
                break
            _merge(paper, _step(paper, name, fn))
    paper["key"] = ids.paper_key(paper)
    return paper


def get_paper(ctx: Ctx, identifier: str) -> dict[str, Any]:
    paper = resolve(ctx, identifier)
    summary = {k: paper.get(k) for k in ("key", "pmid", "pmcid", "doi", "arxiv", "title", "abstract_source")}
    qid = ctx.db.save_query("paper", {"identifier": identifier}, summary, ctx.log.calls)
    out = {"ok": bool(paper["title"]), "query_id": qid, "paper": paper, "api_queries": ctx.log.calls}
    if not paper["title"]:
        out["error"] = "No database returned a record for this identifier (see paper.lookup for each attempt)."
    else:
        out["fulltext_hint"] = (
            "has a PMCID: full text is usually retrievable (fetch_fulltext)" if paper["pmcid"]
            else "preprint: full text is usually retrievable (fetch_fulltext)" if paper["is_preprint"]
            else "no PMCID: full text depends on an open-access copy or a publisher key (fetch_fulltext will tell)")
    return out
