"""The fetch ladder and the search merge, with every HTTP call mocked."""

import hashlib
import io
import json
import zipfile

import httpx
import pytest
import respx

from paperops_mcp import fetch, ncbi, search
from paperops_mcp.environment import check_environment

from conftest import JATS, PUBMED_XML, make_pdf

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
S3 = "https://pmc-oa-opendata.s3.amazonaws.com"
PREFIX = "PMC7054213.1"


def mock_pubmed(pmid="32025018", xml=PUBMED_XML):
    respx.get(f"{EUTILS}/esearch.fcgi").mock(return_value=httpx.Response(200, json={
        "esearchresult": {"count": "1", "idlist": [pmid], "querytranslation": '"brca1"[All Fields]'}}))
    respx.get(f"{EUTILS}/efetch.fcgi").mock(return_value=httpx.Response(200, text=xml))


def s3_listing(names):
    items = "".join(f"<Contents><Key>{PREFIX}/{n}</Key><ETag>x</ETag><Size>10</Size></Contents>" for n in names)
    return f"<ListBucketResult><IsTruncated>false</IsTruncated>{items}</ListBucketResult>"


def mock_s3(pdf: bytes, xml: bytes = JATS.encode(), supp: bytes = b"supplementary table"):
    names = [f"{PREFIX}.xml", f"{PREFIX}.pdf", f"{PREFIX}.json", "41586_2020_1943_MOESM1_ESM.pdf", "Fig1_HTML.jpg"]
    respx.get(f"{S3}/").mock(return_value=httpx.Response(200, text=s3_listing(names)))
    md5 = lambda b: hashlib.md5(b).hexdigest()
    respx.get(f"{S3}/{PREFIX}/{PREFIX}.json").mock(return_value=httpx.Response(200, json={
        "pdf_url": f"s3://pmc-oa-opendata/{PREFIX}/{PREFIX}.pdf?md5={md5(pdf)}",
        "xml_url": f"s3://pmc-oa-opendata/{PREFIX}/{PREFIX}.xml?md5={md5(xml)}",
        "media_urls": [f"s3://pmc-oa-opendata/{PREFIX}/41586_2020_1943_MOESM1_ESM.pdf?md5={md5(supp)}"]}))
    respx.get(f"{S3}/{PREFIX}/{PREFIX}.xml").mock(return_value=httpx.Response(200, content=xml))
    respx.get(f"{S3}/{PREFIX}/{PREFIX}.pdf").mock(return_value=httpx.Response(200, content=pdf))
    respx.get(f"{S3}/{PREFIX}/41586_2020_1943_MOESM1_ESM.pdf").mock(return_value=httpx.Response(200, content=supp))


@respx.mock
def test_open_access_paper_stops_at_first_rung(ctx):
    mock_pubmed()
    mock_s3(make_pdf())
    out = fetch.fetch_fulltext(ctx, "32025018")
    assert out["ok"] and out["got_from"] == ["pmc_s3"] and out["text_from"] == "xml"
    assert [a["source"] for a in out["attempts"]] == ["pmc_s3"]          # the ladder stopped
    assert {f["format"] for f in out["files"]} == {"xml", "pdf"} and all(f["md5_verified"] for f in out["files"])
    assert "## Results" in open(out["text_path"], encoding="utf-8").read()
    # the attempt is in the log, and a second call is served from disk
    assert fetch.fetch_log(ctx, "doi_10.1038_s41586-020-1943-3")["by_reason"] == {"ok": 1}
    assert fetch.fetch_fulltext(ctx, "32025018")["cached"] is True


@respx.mock
def test_corrupted_download_is_rejected_by_md5(ctx):
    mock_pubmed()
    mock_s3(make_pdf())
    respx.get(f"{S3}/{PREFIX}/{PREFIX}.pdf").mock(return_value=httpx.Response(200, content=make_pdf("other text " * 30)))
    out = fetch.fetch_fulltext(ctx, "32025018", sources=["pmc_s3"])
    assert out["ok"] and [f["format"] for f in out["files"]] == ["xml"]
    assert "pdf: invalid_file" in out["attempts"][0]["detail"]


@respx.mock
def test_paywalled_paper_explains_every_step(ctx, monkeypatch):
    no_pmc = PUBMED_XML.replace('<ArticleId IdType="pmc">PMC7054213</ArticleId>', "") \
        .replace("10.1038/s41586-020-1943-3", "10.1016/j.cell.2023.01.035")
    mock_pubmed(xml=no_pmc)
    respx.get("https://api.openalex.org/works/doi:10.1016/j.cell.2023.01.035").mock(return_value=httpx.Response(
        200, json={"doi": "https://doi.org/10.1016/j.cell.2023.01.035", "open_access": {"is_oa": True},
                   "best_oa_location": {"pdf_url": "https://www.cell.com/article/pdf"}}))
    # the "PDF" link answers with a login page: must be reported as paywall, not saved as a paper
    respx.get("https://www.cell.com/article/pdf").mock(return_value=httpx.Response(
        200, headers={"content-type": "text/html"}, text="<!DOCTYPE html><html><body>Sign in</body></html>"))
    respx.get("https://api.semanticscholar.org/graph/v1/paper/DOI:10.1016/j.cell.2023.01.035").mock(
        return_value=httpx.Response(429, json={"message": "Too Many Requests"}))
    out = fetch.fetch_fulltext(ctx, "10.1016/j.cell.2023.01.035")
    got = {a["source"]: (a["status"], a["reason"]) for a in out["attempts"]}
    assert not out["ok"] and not out["files"]
    assert got == {
        "pmc_s3": ("skip", "not_applicable"), "europepmc": ("skip", "not_applicable"),
        "pmc_bioc": ("skip", "not_applicable"), "biorxiv": ("skip", "not_applicable"),
        "arxiv": ("skip", "not_applicable"), "unpaywall": ("skip", "no_email"),
        "openalex": ("fail", "paywall"), "semanticscholar": ("fail", "rate_limited"),
        "elsevier": ("skip", "no_key"), "wiley": ("skip", "not_applicable"),
        "springer": ("skip", "not_applicable"), "publisher": ("skip", "disabled"),
    }
    assert not list((ctx.cfg.data_root / "papers").rglob("*.pdf"))          # nothing bogus left on disk
    assert any("ELSEVIER_API_KEY" in s for s in out["next_steps"])
    assert fetch.fetch_log(ctx, reason="paywall")["n"] == 1

    # with a key that Elsevier knows but does not entitle: a different, specific reason
    monkeypatch.setenv("ELSEVIER_API_KEY", "k" * 32)
    respx.get("https://api.elsevier.com/content/article/doi/10.1016/j.cell.2023.01.035").mock(
        return_value=httpx.Response(403, text="<service-error><statusText>Requestor configuration settings "
                                              "insufficient for access to this resource.</statusText></service-error>"))
    out = fetch.fetch_fulltext(ctx, "10.1016/j.cell.2023.01.035", sources=["elsevier"])
    a = out["attempts"][0]
    assert (a["reason"], a["http_status"]) == ("not_entitled", 403) and "insufficient" in a["detail"]
    assert "k" * 32 not in json.dumps(out) + json.dumps(ctx.db.get_query(out["query_id"]))


@respx.mock
def test_supplement_from_pmc_and_from_europepmc_zip(ctx):
    mock_pubmed()
    mock_s3(make_pdf())
    out = fetch.fetch_supplement(ctx, "32025018")
    assert out["ok"] and [a["source"] for a in out["attempts"]] == ["pmc_s3"]
    names = [f["path"].replace("\\", "/").rsplit("/", 1)[-1] for f in out["files"]]
    assert names == ["41586_2020_1943_MOESM1_ESM.pdf"]                       # the figure jpg is not a supplement

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("table_S1.xlsx", b"x" * 50)
        zf.writestr("../../evil.sh", b"rm -rf")                              # path traversal must be neutralised
    respx.get("https://www.ebi.ac.uk/europepmc/webservices/rest/PMC7054213/supplementaryFiles").mock(
        return_value=httpx.Response(200, content=buf.getvalue()))
    out = fetch.fetch_supplement(ctx, "32025018", sources=["europepmc"])
    supp_dir = ctx.cfg.data_root / "papers" / "doi_10.1038_s41586-020-1943-3" / "supplement"
    assert out["ok"] and (supp_dir / "table_S1.xlsx").exists() and (supp_dir / "evil.sh").exists()
    assert not (ctx.cfg.data_root.parent / "evil.sh").exists() and not (supp_dir / "_europepmc.zip").exists()


@respx.mock
def test_search_logs_keywords_and_survives_a_dead_source(ctx):
    mock_pubmed()
    respx.get("https://www.ebi.ac.uk/europepmc/webservices/rest/search").mock(return_value=httpx.Response(503))
    respx.get("https://api.crossref.org/works").mock(return_value=httpx.Response(200, json={"message": {
        "total-results": 2, "items": [
            {"DOI": "10.1038/S41586-020-1943-3", "title": ["The repertoire of mutational signatures in human cancer"],
             "issued": {"date-parts": [[2020, 2]]}, "abstract": "<jats:p>From Crossref.</jats:p>"},
            {"DOI": "10.1101/322859", "title": ["The repertoire of mutational signatures in human cancer"],
             "type": "posted-content", "issued": {"date-parts": [[2018]]}}]}}))
    out = search.search_papers(ctx, "brca1", sources=["pubmed", "europepmc", "crossref"],
                               source_queries={"pubmed": "BRCA1[tiab]"})
    assert out["ok"] and "europepmc" in out["warning"]
    assert out["sources"]["pubmed"]["query_sent"] == "BRCA1[tiab]"
    assert out["sources"]["pubmed"]["interpreted_as"] == '"brca1"[All Fields]'
    assert out["sources"]["europepmc"] == {"status": "fail", "reason": "server_error", "detail": "",
                                           "query_sent": "brca1"}
    # same DOI from two sources is one result; the preprint with the same title stays separate
    assert out["n_unique"] == 2
    top = out["results"][0]
    assert top["found_in"] == ["pubmed", "crossref"] and top["pmid"] == "32025018"
    stored = ctx.db.get_query(out["query_id"])
    assert stored["params"]["query"] == "brca1" and len(stored["api_queries"]) == 4
    full = json.loads(open(out["result_path"], encoding="utf-8").read())
    assert full["results"][0]["abstract"].startswith("BACKGROUND")


def test_merge_by_pmid_then_doi():
    merged = search.merge({
        "a": [{"pmid": "1", "doi": None, "title": "T", "authors": ["X"]}],
        "b": [{"pmid": "1", "doi": "10.1/a", "title": "T", "abstract": "abs"}],
        "c": [{"pmid": None, "doi": "10.1/a", "title": "T"}, {"pmid": None, "doi": None, "title": "Other"}],
    })
    assert len(merged) == 2 and merged[0]["found_in"] == ["a", "b", "c"]
    assert merged[0]["doi"] == "10.1/a" and merged[0]["abstract"] == "abs"


def test_environment_without_probe_names_what_needs_a_key(ctx, monkeypatch):
    monkeypatch.setenv("SPRINGER_API_KEY", "secret-value")
    out = check_environment(ctx)
    by_id = {d["id"]: d for d in out["databases"]}
    assert by_id["elsevier"]["status"] == "needs_key" and by_id["unpaywall"]["status"] == "needs_email"
    assert by_id["springer"]["status"] == "not probed" and out["keys"]["SPRINGER_API_KEY"]["set"] is True
    assert "secret-value" not in json.dumps(out)


@respx.mock
def test_s3_picks_the_latest_version(ctx):
    listing = ("<ListBucketResult><IsTruncated>false</IsTruncated>"
               "<Contents><Key>PMC1.1/PMC1.1.xml</Key><Size>5</Size></Contents>"
               "<Contents><Key>PMC1.2/PMC1.2.xml</Key><Size>7</Size></Contents></ListBucketResult>")
    respx.get(f"{S3}/").mock(return_value=httpx.Response(200, text=listing))
    assert ncbi.s3_list(ctx, "PMC1") == {"prefix": "PMC1.2", "version": 2, "files": {"PMC1.2.xml": 7}}
    respx.get(f"{S3}/").mock(return_value=httpx.Response(200, text="<ListBucketResult></ListBucketResult>"))
    with pytest.raises(Exception) as e:
        ncbi.s3_list(ctx, "PMC9")
    assert e.value.reason == "not_found"
