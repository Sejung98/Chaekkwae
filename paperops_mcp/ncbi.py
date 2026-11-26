"""NCBI sources: PubMed (E-utilities), PubTator3, the PMC ID converter, PMC on AWS, and the BioC API.

Checked against the live services on 2026-11-20:
- E-utilities esearch/efetch: https://www.ncbi.nlm.nih.gov/books/NBK25499/
- PMC ID converter moved to https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/
- The PMC OA web service (oa.fcgi) and FTP are gone; articles are in the public S3 bucket
  pmc-oa-opendata as PMC<id>.<version>/PMC<id>.<version>.{xml,pdf,txt,json} plus media files.
  The .json file lists md5 checksums for every file.
- BioC: https://www.ncbi.nlm.nih.gov/research/bionlp/RESTful/pmcoa.cgi/BioC_json/<PMID|PMCID>/unicode
- PubTator3 search: https://www.ncbi.nlm.nih.gov/research/pubtator3-api/search/?text=...
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

from .catalog import key
from .core import Ctx, record
from .http import SourceError
from .textio import squash

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
IDCONV = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"
S3 = "https://pmc-oa-opendata.s3.amazonaws.com"
BIOC = "https://www.ncbi.nlm.nih.gov/research/bionlp/RESTful/pmcoa.cgi/BioC_json"
PUBTATOR = "https://www.ncbi.nlm.nih.gov/research/pubtator3-api/search/"


def _params(ctx: Ctx, **extra: Any) -> dict[str, Any]:
    p: dict[str, Any] = {"tool": "saseo", **extra}
    if ctx.cfg.email:
        p["email"] = ctx.cfg.email
    if key("NCBI_API_KEY"):
        p["api_key"] = key("NCBI_API_KEY")
    return p


def _get(ctx: Ctx, url: str, params: dict):
    return ctx.get(url, params=params, auth="api_key" in params)


# ---------------------------------------------------------------- PubMed
def pubmed_ids(ctx: Ctx, term: str, max_results: int = 20, year_from: int | None = None,
               year_to: int | None = None, sort: str = "relevance") -> dict[str, Any]:
    p = _params(ctx, db="pubmed", term=term, retmode="json", retmax=max_results,
                sort="pub_date" if sort == "date" else "relevance")
    if year_from or year_to:
        p.update(datetype="pdat", mindate=str(year_from or 1800), maxdate=str(year_to or 3000))
    res = _get(ctx, f"{EUTILS}/esearch.fcgi", p).json().get("esearchresult", {})
    if res.get("ERROR"):
        raise SourceError("http_error", f"PubMed: {res['ERROR']}"[:200])
    return {"pmids": res.get("idlist", []), "total": int(res.get("count", 0)),
            "translation": res.get("querytranslation"), "warnings": res.get("warninglist")}


def pubmed_fetch(ctx: Ctx, pmids: list[str]) -> list[dict[str, Any]]:
    if not pmids:
        return []
    xml = _get(ctx, f"{EUTILS}/efetch.fcgi", _params(ctx, db="pubmed", id=",".join(pmids), retmode="xml")).content
    by_id = {r["pmid"]: r for r in parse_pubmed_xml(xml)}
    return [by_id[p] for p in pmids if p in by_id]


def parse_pubmed_xml(xml: bytes) -> list[dict[str, Any]]:
    out = []
    for art in ET.fromstring(xml).iter("PubmedArticle"):
        mc = art.find("MedlineCitation")
        a = mc.find("Article") if mc is not None else None
        if a is None:
            continue
        parts = []
        for at in a.findall("Abstract/AbstractText"):
            text = squash("".join(at.itertext()))
            if text:
                parts.append(f"{at.get('Label')}: {text}" if at.get("Label") else text)
        authors = []
        for au in a.findall("AuthorList/Author"):
            name = " ".join(x for x in (au.findtext("LastName"), au.findtext("Initials")) if x)
            authors.append(name or au.findtext("CollectiveName") or "")
        ids = {i.get("IdType"): (i.text or "").strip() for i in art.findall("PubmedData/ArticleIdList/ArticleId")}
        pub = a.find("Journal/JournalIssue/PubDate")
        year = (pub.findtext("Year") or (pub.findtext("MedlineDate") or "")[:4]) if pub is not None else None
        title_el = a.find("ArticleTitle")
        out.append(record(
            "pubmed", pmid=mc.findtext("PMID"), pmcid=ids.get("pmc"), doi=ids.get("doi"),
            title=squash("".join(title_el.itertext())) if title_el is not None else None,
            authors=[x for x in authors if x], journal=a.findtext("Journal/ISOAbbreviation") or a.findtext("Journal/Title"),
            year=year, abstract="\n".join(parts) or None,
            pub_types=[t.text for t in a.findall("PublicationTypeList/PublicationType") if t.text],
            mesh=[d.text for d in mc.findall("MeshHeadingList/MeshHeading/DescriptorName") if d.text],
        ))
    return out


def pubmed_search(ctx: Ctx, query: str, max_results: int, year_from: int | None, year_to: int | None,
                  sort: str) -> tuple[list[dict], dict]:
    found = pubmed_ids(ctx, query, max_results, year_from, year_to, sort)
    info = {"query_sent": query, "total": found["total"], "interpreted_as": found["translation"]}
    if found.get("warnings"):
        info["warnings"] = found["warnings"]
    return pubmed_fetch(ctx, found["pmids"]), info


def pmid_for_doi(ctx: Ctx, doi: str) -> str | None:
    found = pubmed_ids(ctx, f'"{doi}"[AID]', max_results=2)
    return found["pmids"][0] if len(found["pmids"]) == 1 else None


# ---------------------------------------------------------------- PubTator3
def pubtator_search(ctx: Ctx, query: str, max_results: int, year_from: int | None, year_to: int | None,
                    sort: str) -> tuple[list[dict], dict]:
    data = ctx.get(PUBTATOR, params={"text": query, "page": 1}).json()
    recs = []
    for r in (data.get("results") or [])[:max_results]:
        year = (r.get("meta_date_publication") or r.get("date") or "")[:4]
        if (year_from and year and int(year) < year_from) or (year_to and year and int(year) > year_to):
            continue
        recs.append(record("pubtator", pmid=r.get("pmid"), pmcid=r.get("pmcid"), doi=r.get("doi"),
                           title=r.get("title"), authors=r.get("authors") or [], journal=r.get("journal"),
                           year=year or None))
    return recs, {"query_sent": query, "total": data.get("count"),
                  "note": "first page only (10 per page); year filter applied locally; abstracts come from get_paper"}


def pubtator_entities(ctx: Ctx, text: str, limit: int = 8) -> list[dict[str, Any]]:
    """Entity ids for a gene / variant / disease / chemical name, to use in a pubtator query."""
    rows = ctx.get(PUBTATOR.replace("/search/", "/entity/autocomplete/"), params={"query": text, "limit": limit}).json()
    return [{"entity": r.get("_id"), "type": r.get("biotype"), "name": r.get("name"),
             "description": r.get("description")} for r in rows]


# ---------------------------------------------------------------- ID converter
def idconv(ctx: Ctx, identifier: str) -> dict[str, str]:
    """PMID / PMCID / DOI of an article that is in PMC. Empty dict if PMC does not have it."""
    data = _get(ctx, IDCONV, _params(ctx, ids=identifier, format="json")).json()
    rec = (data.get("records") or [{}])[0]
    if rec.get("status") == "error":
        return {}
    return {k: str(rec[k]) for k in ("pmid", "pmcid", "doi") if rec.get(k)}


# ---------------------------------------------------------------- PMC on AWS
_KEY = re.compile(r"<Key>([^<]+)</Key>.*?<Size>(\d+)</Size>", re.S)
_TOKEN = re.compile(r"<NextContinuationToken>([^<]+)</NextContinuationToken>")


def s3_list(ctx: Ctx, pmcid: str) -> dict[str, Any]:
    """Files of the latest version of an article in the PMC open bucket: {prefix, version, files{name: size}}."""
    params: dict[str, Any] = {"list-type": 2, "prefix": f"{pmcid}."}
    keys: dict[str, int] = {}
    for _ in range(5):  # 1000 keys per page; no article comes close to 5000 files
        xml = ctx.get(f"{S3}/", params=params).text
        keys.update({k: int(s) for k, s in _KEY.findall(xml)})
        m = _TOKEN.search(xml)
        if "<IsTruncated>true" not in xml or not m:
            break
        params["continuation-token"] = m.group(1)
    versions = {int(m.group(1)) for k in keys if (m := re.match(rf"{pmcid}\.(\d+)/", k))}
    if not versions:
        raise SourceError("not_found", f"{pmcid} is not in the PMC open bucket (PMC shows it on its website "
                                       "only: no reuse licence, or still under embargo)")
    prefix = f"{pmcid}.{max(versions)}"
    return {"prefix": prefix, "version": max(versions),
            "files": {k.split("/", 1)[1]: s for k, s in keys.items() if k.startswith(prefix + "/")}}


def s3_url(prefix: str, name: str) -> str:
    return f"{S3}/{prefix}/{name}"


def s3_md5(ctx: Ctx, prefix: str) -> dict[str, str]:
    """{file name: md5} from the article's metadata json. Empty if the json is missing."""
    try:
        meta = ctx.get(s3_url(prefix, f"{prefix}.json")).json()
    except (SourceError, ValueError):
        return {}
    urls = [meta.get(k) for k in ("pdf_url", "xml_url", "text_url")] + list(meta.get("media_urls") or [])
    out = {}
    for u in urls:
        m = re.search(r"/([^/?]+)\?md5=([0-9a-f]{32})$", u or "")
        if m:
            out[m.group(1)] = m.group(2)
    return out


def bioc_url(identifier: str) -> str:
    return f"{BIOC}/{identifier}/unicode"
