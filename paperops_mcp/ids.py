"""Recognising paper identifiers: PMID, PMCID, DOI, arXiv id, and the common URL forms of each."""

from __future__ import annotations

import re
from urllib.parse import unquote

_DOI = re.compile(r"(10\.\d{4,9}/[^\s\"<>]+)", re.I)
_PMCID = re.compile(r"^PMC(\d+)(?:\.\d+)?$", re.I)
_PMID = re.compile(r"^\d{1,9}$")
_ARXIV = re.compile(r"^(?:arxiv:)?(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?$", re.I)
_URL_PMID = re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)|ncbi\.nlm\.nih\.gov/pubmed/(\d+)", re.I)
_URL_PMC = re.compile(r"/(?:pmc/)?articles/(PMC\d+)", re.I)
_URL_ARXIV = re.compile(r"arxiv\.org/(?:abs|pdf)/([^\s?#]+?)(?:\.pdf)?(?:[?#].*)?$", re.I)
_ARXIV_DOI = re.compile(r"^10\.48550/arxiv\.(.+)$", re.I)
# bioRxiv/medRxiv DOIs: 10.1101/2020.01.01.123456 or the older 10.1101/123456; since 2026 also 10.64898/...
_PREPRINT_DOI = re.compile(r"^(10\.1101/(?:\d{4}\.\d{2}\.\d{2}\.)?\d{6,8}|10\.64898/.+)$")
_PREPRINT_URL_TAIL = re.compile(r"v\d+(?:\.(?:full|abstract|supplementary-material)\S*)?$")


def parse(identifier: str) -> dict[str, str]:
    """Return one of {'pmid'|'pmcid'|'doi'|'arxiv': value}. Raises ValueError if nothing is recognised."""
    s = unquote(str(identifier).strip())
    m = _URL_PMID.search(s)
    if m:
        return {"pmid": m.group(1) or m.group(2)}
    m = _URL_PMC.search(s)
    if m:
        return {"pmcid": m.group(1).upper()}
    m = _URL_ARXIV.search(s)
    if m:
        s = m.group(1)
    low = s.lower()
    for prefix in ("pmid:", "pmid ", "doi:", "doi "):
        if low.startswith(prefix):
            s = s[len(prefix):].strip()
    m = _PMCID.match(s)
    if m:
        return {"pmcid": f"PMC{m.group(1)}"}
    if _PMID.match(s):
        return {"pmid": s}
    m = _ARXIV.match(s)
    if m:
        return {"arxiv": m.group(1)}
    m = _DOI.search(s)
    if m:
        doi = m.group(1).rstrip(".,;)]}>'\"").lower()
        if "rxiv.org/content/" in low:  # a bioRxiv/medRxiv URL carries the version after the DOI
            doi = _PREPRINT_URL_TAIL.sub("", doi)
        am = _ARXIV_DOI.match(doi)
        if am:
            return {"arxiv": re.sub(r"v\d+$", "", am.group(1))}
        return {"doi": doi}
    raise ValueError(f"Not a PMID, PMCID, DOI or arXiv id: {identifier!r}")


def is_preprint_doi(doi: str | None) -> bool:
    return bool(doi and _PREPRINT_DOI.match(doi.lower()))


def paper_key(ids: dict) -> str:
    """Stable folder name for a paper: DOI first, then PMID, PMCID, arXiv id."""
    if ids.get("doi"):
        return "doi_" + re.sub(r"[^A-Za-z0-9._-]", "_", ids["doi"])[:150]
    if ids.get("pmid"):
        return "pmid_" + re.sub(r"[^0-9]", "", str(ids["pmid"]))
    if ids.get("pmcid"):
        return "PMC" + re.sub(r"[^0-9]", "", str(ids["pmcid"]))
    if ids.get("arxiv"):
        return "arxiv_" + re.sub(r"[^A-Za-z0-9._-]", "_", ids["arxiv"])
    raise ValueError("paper has no identifier")


def norm_title(title: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()
