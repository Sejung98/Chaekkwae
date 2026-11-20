"""Identifier parsing, secret masking, failure reasons, file validation, catalog consistency."""

import httpx
import pytest
import respx

from paperops_mcp import catalog, http, ids, ncbi, textio
from paperops_mcp.environment import PROBES
from paperops_mcp.fetch import FULLTEXT_LADDER
from paperops_mcp.http import SourceError

from conftest import JATS, PUBMED_XML, make_pdf


@pytest.mark.parametrize("given, expected", [
    ("32025018", {"pmid": "32025018"}),
    ("PMID: 32025018", {"pmid": "32025018"}),
    ("https://pubmed.ncbi.nlm.nih.gov/32025018/", {"pmid": "32025018"}),
    ("pmc7054213", {"pmcid": "PMC7054213"}),
    ("https://pmc.ncbi.nlm.nih.gov/articles/PMC7054213/", {"pmcid": "PMC7054213"}),
    ("10.1038/s41586-020-1943-3", {"doi": "10.1038/s41586-020-1943-3"}),
    ("https://doi.org/10.1038/S41586-020-1943-3.", {"doi": "10.1038/s41586-020-1943-3"}),
    ("https://www.biorxiv.org/content/10.1101/2021.05.16.444385v2.full.pdf", {"doi": "10.1101/2021.05.16.444385"}),
    ("arXiv:2106.14599v2", {"arxiv": "2106.14599"}),
    ("https://arxiv.org/abs/2106.14599", {"arxiv": "2106.14599"}),
    ("10.48550/arXiv.2106.14599", {"arxiv": "2106.14599"}),
    ("q-bio/0401001", {"arxiv": "q-bio/0401001"}),
])
def test_parse_identifier(given, expected):
    assert ids.parse(given) == expected


def test_parse_rejects_free_text():
    with pytest.raises(ValueError):
        ids.parse("mutational signatures review")


def test_preprint_doi_is_not_a_cshl_journal_doi():
    assert ids.is_preprint_doi("10.1101/2021.05.16.444385")
    assert ids.is_preprint_doi("10.1101/444385")
    assert ids.is_preprint_doi("10.64898/2026.11.12.766009")
    assert not ids.is_preprint_doi("10.1101/gr.276013.121")  # Genome Research shares the 10.1101 prefix


def test_paper_key_is_a_safe_folder_name():
    key = ids.paper_key({"doi": "10.1002/(sici)1097-0142(19990101)85:1<1::aid>3.0.co;2-x"})
    assert key.startswith("doi_10.1002_") and not set(key) & set('<>:"/\\|?*();')


def test_secrets_are_masked_in_the_api_log():
    log = http.ApiLog()
    log.add("GET", "https://api.unpaywall.org/v2/10.1/x?email=me@lab.org&api_key=SECRET1",
            {"api_key": "SECRET2", "term": "brca1", "email": "me@lab.org"}, 200)
    text = str(log.calls)
    assert "SECRET" not in text and "me@lab.org" not in text and "brca1" in text


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://host/x", "http://localhost/x", "http://127.0.0.1/x",
                                 "http://10.0.0.5/x", "http://169.254.169.254/latest/meta-data", "s3://bucket/key"])
def test_non_public_urls_are_refused(url):
    with pytest.raises(SourceError) as e:
        http.check_url(url)
    assert e.value.reason == "blocked_url"


@respx.mock
@pytest.mark.parametrize("status, headers, body, auth, reason", [
    (429, {}, "slow down", False, "rate_limited"),
    (503, {}, "down", False, "server_error"),
    (404, {}, "no", False, "not_found"),
    (403, {}, "<html>please log in</html>", False, "paywall"),
    (403, {"cf-mitigated": "challenge"}, "<html><title>Just a moment...</title></html>", False, "bot_wall"),
    (401, {}, "bad key", True, "key_rejected"),
])
def test_http_status_becomes_a_reason(status, headers, body, auth, reason):
    respx.get("https://api.example.org/x").mock(return_value=httpx.Response(status, headers=headers, text=body))
    with httpx.Client() as client, pytest.raises(SourceError) as e:
        http.request(client, http.ApiLog(), "GET", "https://api.example.org/x", retries=0, auth=auth)
    assert e.value.reason == reason and e.value.status == status


@respx.mock
def test_timeout_and_retry():
    route = respx.get("https://api.example.org/x").mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json={"ok": 1})])
    log = http.ApiLog()
    with httpx.Client() as client:
        resp = http.request(client, log, "GET", "https://api.example.org/x", retries=1, sleep=lambda s: None)
    assert resp.json() == {"ok": 1} and route.call_count == 2
    assert [c["status"] for c in log.calls] == [None, 200]


def test_pdf_validation(tmp_path):
    good = tmp_path / "a.pdf"
    good.write_bytes(make_pdf())
    info = textio.pdf_info(good, "The repertoire of mutational signatures in human cancer")
    assert info["ok"] and info["pages"] == 1 and "mutational signatures" in info["text"] and info["title_match"]
    assert textio.pdf_info(good, "Completely unrelated proteomics workflow")["title_match"] is False

    page = tmp_path / "b.pdf"
    page.write_bytes(b"<!DOCTYPE html><html><body>Sign in to continue</body></html>")
    assert not textio.pdf_info(page)["ok"]
    assert http.classify_not_a_file(page).reason == "paywall"
    page.write_bytes(b"<html><title>Just a moment...</title></html>")
    assert http.classify_not_a_file(page).reason == "bot_wall"

    cut = tmp_path / "c.pdf"
    cut.write_bytes(make_pdf()[:200])
    assert not textio.pdf_info(cut)["ok"]


def test_jats_to_text():
    out = textio.jats_to_text(JATS.encode())
    assert out["has_body"] and out["title"].startswith("The repertoire")
    assert "## Results" in out["text"] and "### Signatures" in out["text"] and "## Abstract" in out["text"]
    assert out["supplement_hrefs"] == ["41586_2020_1943_MOESM1_ESM.pdf"]
    abstract_only = JATS.split("<body>")[0] + "</article>"
    assert not textio.jats_to_text(abstract_only.encode())["has_body"]
    broken = JATS.replace("</article>", "&nbsp;")  # undefined entity + unclosed root: still gives text
    assert textio.jats_to_text(broken.encode())["has_body"]


def test_pubmed_xml_parsing_ignores_reference_ids():
    rec = ncbi.parse_pubmed_xml(PUBMED_XML.encode())[0]
    assert rec["pmid"] == "32025018" and rec["doi"] == "10.1038/s41586-020-1943-3" and rec["pmcid"] == "PMC7054213"
    assert rec["title"] == "The repertoire of mutational signatures in human cancer."
    assert rec["abstract"] == "BACKGROUND: Somatic mutations.\nMore text."
    assert rec["authors"] == ["Alexandrov LB", "PCAWG Consortium"] and rec["year"] == 2020 and rec["mesh"] == ["Mutation"]


def test_catalog_is_consistent_with_the_code():
    db_ids = {d["id"] for d in catalog.DATABASES}
    assert set(PROBES) == db_ids                                   # every database can be probed
    assert {n for n, _ in FULLTEXT_LADDER} <= db_ids               # every fetch step is documented
    for d in catalog.DATABASES:
        assert set(d["key_env"]) <= set(catalog.KEYS)
        assert d["key"].split()[0] in ("none", "optional", "required", "e-mail")
    assert all(r in http.REASONS for r in ("no_key", "paywall", "bot_wall", "not_entitled", "internal_error"))


def test_readme_tables_match_the_catalog():
    """The database tables in both READMEs are generated: python -m paperops_mcp.cli databases --markdown"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for block in catalog.markdown_table().split("\n\n"):
        assert block in (root / "README.md").read_text(encoding="utf-8")
    # the Korean README translates the prose but must list every database id and key name
    ko = (root / "README.ko.md").read_text(encoding="utf-8")
    for d in catalog.DATABASES:
        assert f"`{d['id']}`" in ko
    for name in catalog.KEYS:
        assert name in ko


@respx.mock
def test_arxiv_query_building_and_parsing(ctx):
    from paperops_mcp import preprints

    atom = """<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"
      xmlns:arxiv="http://arxiv.org/schemas/atom"><opensearch:totalResults>1</opensearch:totalResults>
      <entry><id>http://arxiv.org/abs/2106.14599v2</id><published>2021-06-28T00:00:00Z</published>
      <title>A  title\n across lines</title><summary> An abstract. </summary>
      <author><name>A One</name></author><author><name>B Two</name></author></entry></feed>"""
    route = respx.get("https://export.arxiv.org/api/query").mock(return_value=httpx.Response(200, text=atom))
    recs, info = preprints.arxiv_search(ctx, "mutational signature", 5, 2024, None, "relevance")
    assert info["query_sent"].startswith("(all:mutational AND all:signature) AND submittedDate:[2024")
    assert info["total"] == 1 and route.called
    assert recs[0]["arxiv"] == "2106.14599" and recs[0]["doi"] == "10.48550/arxiv.2106.14599"
    assert recs[0]["title"] == "A title across lines" and recs[0]["authors"] == ["A One", "B Two"]
    _, info = preprints.arxiv_search(ctx, "cat:q-bio.GN AND all:signature", 5, None, None, "date")
    assert info["query_sent"] == "cat:q-bio.GN AND all:signature"


@respx.mock
def test_redirect_to_a_private_address_is_refused(ctx, tmp_path):
    respx.get("https://oa.example.org/paper.pdf").mock(
        return_value=httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"}))
    internal = respx.get("http://169.254.169.254/latest/meta-data").mock(return_value=httpx.Response(200, text="x"))
    with pytest.raises(SourceError) as e:
        ctx.save("https://oa.example.org/paper.pdf", tmp_path / "a.pdf")
    assert e.value.reason == "blocked_url" and not internal.called


def test_host_names_resolving_to_private_addresses_are_refused(monkeypatch):
    monkeypatch.setattr(http, "_resolve", lambda host: {"127.0.0.1"})
    with pytest.raises(SourceError):
        http.check_url("http://2130706433/x")       # decimal form of 127.0.0.1
    monkeypatch.setattr(http, "_resolve", lambda host: {"93.184.216.34"})
    http.check_url("https://public.example.org/x")


@respx.mock
def test_key_headers_do_not_follow_a_redirect_to_another_site(ctx):
    first = respx.get("https://api.wiley.com/onlinelibrary/tdm/v1/articles/x").mock(
        return_value=httpx.Response(302, headers={"location": "https://cdn.example.net/file.pdf"}))
    other = respx.get("https://cdn.example.net/file.pdf").mock(return_value=httpx.Response(200, content=b"%PDF-"))
    ctx.get("https://api.wiley.com/onlinelibrary/tdm/v1/articles/x", headers={"Wiley-TDM-Client-Token": "tok-123456"})
    assert first.calls[0].request.headers["wiley-tdm-client-token"] == "tok-123456"
    assert "wiley-tdm-client-token" not in other.calls[0].request.headers


def test_secrets_are_scrubbed_from_error_text(monkeypatch):
    monkeypatch.setenv("ELSEVIER_API_KEY", "abcdef0123456789\r\n")
    err = SourceError("network_error", "LocalProtocolError: Illegal header value b'abcdef0123456789\\r'")
    assert "abcdef0123456789" not in err.detail and "***" in err.detail and "abcdef" not in str(err)
    assert catalog.key("ELSEVIER_API_KEY") == "abcdef0123456789"   # stray line ending is stripped
    with http.make_client(email="someone@lab.example") as client:
        assert "someone@lab.example" not in client.headers["user-agent"]
    assert "someone@lab.example" not in SourceError("http_error", "bad email someone@lab.example").detail


def test_file_names_from_remote_sources_are_neutralised():
    from paperops_mcp.fetch import _safe_name

    assert _safe_name("x/..") == "file" and _safe_name("../../etc/passwd") == "passwd"
    assert _safe_name("C:\\Users\\a\\table S1.xlsx") == "table_S1.xlsx"
    assert ids.paper_key({"pmcid": "PMC12/../x"}) == "PMC12"
