"""Smoke tests against the real APIs. Excluded from the default run; run with `pytest -m live -s`.

Example paper: Alexandrov et al., Nature 2020 (PMID 32025018, PMC7054213, open access).
A database that is merely busy (rate_limited / timeout / server_error) is reported as a skip, not a failure:
these tests check this project's code, not the uptime of third parties.
"""

import pytest

from paperops_mcp import fetch, papers, search
from paperops_mcp.environment import check_environment

pytestmark = pytest.mark.live
BUSY = {"rate_limited", "timeout", "server_error", "network_error"}
NEEDS_SETUP = {"needs_key", "needs_email", "off"}


@pytest.fixture(autouse=True)
def _live_settings(cfg):
    cfg.data["http"].update(retries=1, timeout=60)


def test_every_keyless_database_answers(ctx):
    out = check_environment(ctx, probe=True)
    for row in out["databases"]:
        print(f"{row['id']:16} {row['status']:12} {row.get('reason', ''):14} {row['seconds']}s")
    broken = [r["id"] for r in out["databases"]
              if r["status"] == "fail" and r["reason"] not in BUSY]
    assert not broken, f"these databases fail for a reason other than being busy: {broken}"
    assert {r["id"] for r in out["databases"] if r["status"] in NEEDS_SETUP} >= {"elsevier", "wiley", "springer"}


def test_pubmed_search_and_paper(ctx):
    # Free text can be misread: PubMed turned "... in human cancer" into the author "human, in[Author]" and
    # found nothing. The log shows that in `interpreted_as`; title words with a field tag fix it.
    # (A long quoted title is no better: PubMed answers "quoted phrase not found", which lands in `warnings`.)
    loose = search.search_papers(ctx, "Alexandrov repertoire of mutational signatures in human cancer",
                                 sources=["pubmed"], max_results=5, year_from=2020, year_to=2020)
    print("free text ->", loose["sources"]["pubmed"]["total"], "hits")
    assert loose["sources"]["pubmed"]["status"] == "ok" and loose["sources"]["pubmed"]["interpreted_as"]
    out = search.search_papers(ctx, "repertoire mutational signatures human cancer[ti]",
                               sources=["pubmed"], max_results=5, year_from=2020, year_to=2020)
    assert any(r["pmid"] == "32025018" for r in out["results"])
    paper = papers.get_paper(ctx, "10.1038/s41586-020-1943-3")["paper"]
    assert paper["pmid"] == "32025018" and paper["pmcid"] == "PMC7054213" and len(paper["abstract"]) > 500


def test_fulltext_and_supplement_of_an_open_access_paper(ctx):
    out = fetch.fetch_fulltext(ctx, "32025018", want_pdf=True)
    assert out["ok"] and out["text_chars"] > 20000
    pdf = next(f for f in out["files"] if f["format"] == "pdf")
    assert pdf["pages"] > 5 and pdf["title_match"] and pdf["md5_verified"]
    supp = fetch.fetch_supplement(ctx, "32025018")
    assert supp["ok"] and len(supp["files"]) >= 1


def test_paper_outside_pmc_fails_with_reasons_not_with_a_fake_file(ctx):
    out = fetch.fetch_fulltext(ctx, "10.1016/j.cell.2023.01.035")
    reasons = {a["source"]: a["reason"] for a in out["attempts"]}
    print(reasons)
    assert reasons["pmc_s3"] == "not_applicable" and reasons["elsevier"] == "no_key"
    if not out["ok"]:
        assert out["why"] and out["next_steps"]
