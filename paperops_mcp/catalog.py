"""Catalog of literature databases: what each one gives (search / abstract / full text / supplement)
and whether it needs an API key.

This is the single source for `list_databases`, `check_environment` and the README table
(`python -m paperops_mcp.cli databases --markdown`). Every row was checked against the live API
on the date in VERIFIED. It contains no paper counts; numbers always come from live tool results.
"""

from __future__ import annotations

import os
from typing import Any

VERIFIED = "2026-11-20"

# Environment variables that hold credentials. Values are never printed, logged or written to disk.
KEYS: dict[str, dict[str, str]] = {
    "NCBI_API_KEY": {"for": "PubMed / PMC E-utilities (optional: 10 requests/s instead of 3)",
                     "get": "https://account.ncbi.nlm.nih.gov/settings/ (free, instant)"},
    "OPENALEX_API_KEY": {"for": "OpenAlex (optional: own daily budget instead of one shared per IP)",
                         "get": "https://openalex.org/settings/api (free, instant)"},
    "S2_API_KEY": {"for": "Semantic Scholar (optional, but keyword search is usually rate-limited without it)",
                   "get": "https://www.semanticscholar.org/product/api#api-key-form (free, reviewed by hand)"},
    "ELSEVIER_API_KEY": {"for": "Elsevier / ScienceDirect full text (required)",
                         "get": "https://dev.elsevier.com/ (free key; it must also be linked to a subscribing "
                                "institution, by IP or by ELSEVIER_INSTTOKEN from your library)"},
    "ELSEVIER_INSTTOKEN": {"for": "Elsevier institutional token (only if your IP is not recognised)",
                           "get": "ask your library to request it from Elsevier"},
    "WILEY_TDM_TOKEN": {"for": "Wiley Online Library full-text PDFs (required)",
                        "get": "https://onlinelibrary.wiley.com/library-info/resources/text-and-datamining "
                               "(free with a Wiley account; subscribed content needs an institutional subscription)"},
    "SPRINGER_API_KEY": {"for": "Springer Nature Open Access API (required)",
                         "get": "https://dev.springernature.com/ (free, instant)"},
}


def key(name: str) -> str | None:
    return os.environ.get(name, "").strip() or None


DATABASES: list[dict[str, Any]] = [
    {
        "id": "pubmed", "name": "PubMed (NCBI E-utilities)",
        "covers": "biomedical journal articles (MEDLINE + PMC), with MeSH terms",
        "search": "yes (PubMed query syntax, field tags, MeSH)", "abstract": "yes", "fulltext": "no",
        "supplement": "no", "key": "optional", "key_env": ["NCBI_API_KEY"],
        "notes": "The search log keeps PubMed's own translation of your keywords (querytranslation).",
        "checked": "search, abstract: OK",
        "api": "https://www.ncbi.nlm.nih.gov/books/NBK25497/",
    },
    {
        "id": "europepmc", "name": "Europe PMC",
        "covers": "PubMed + PMC + preprints (bioRxiv, medRxiv, Research Square, ...) + patents, in one index",
        "search": "yes (full-text search too)", "abstract": "yes",
        "fulltext": "JATS XML for the open-access subset only", "supplement": "zip, open-access subset only",
        "key": "none", "key_env": [],
        "notes": "Can be slow (tens of seconds per request at busy times); timeouts are logged as such.",
        "checked": "search, full-text XML, supplement zip: OK, but slow and unstable that day (single requests took up to 47 s; some timed out or returned HTTP 503)",
        "api": "https://europepmc.org/RestfulWebService",
    },
    {
        "id": "preprints", "name": "Preprint keyword search (Europe PMC SRC:PPR)",
        "covers": "bioRxiv, medRxiv and other preprint servers indexed by Europe PMC",
        "search": "yes", "abstract": "yes", "fulltext": "see biorxiv / arxiv", "supplement": "no",
        "key": "none", "key_env": [],
        "notes": "The bioRxiv API itself has no keyword search, so preprint search goes through Europe PMC.",
        "checked": "search: OK",
        "api": "https://europepmc.org/Preprints",
    },
    {
        "id": "pubtator", "name": "PubTator3 (NCBI)",
        "covers": "PubMed/PMC with gene, variant, disease, chemical, species and cell-line annotations",
        "search": "yes (entity search, e.g. @GENE_BRCA1 AND @DISEASE_Breast_Neoplasms)",
        "abstract": "via pubmed", "fulltext": "no (use pmc_bioc)", "supplement": "no",
        "key": "none", "key_env": [],
        "notes": "Best when the question is about a gene, variant or disease rather than free text.",
        "checked": "search, entity lookup: OK",
        "api": "https://www.ncbi.nlm.nih.gov/research/pubtator3/api",
    },
    {
        "id": "pmc_s3", "name": "PMC Open Access on AWS (pmc-oa-opendata)",
        "covers": "PMC open-access articles and author manuscripts",
        "search": "no", "abstract": "no (use pubmed)", "fulltext": "JATS XML + PDF + plain text, with md5",
        "supplement": "yes (individual files, with md5)", "key": "none", "key_env": [],
        "notes": "Fastest full-text route; needs a PMCID. The old OA web service (oa.fcgi) answered HTTP 404 on the check date.",
        "checked": "XML, PDF, supplement files: OK, md5 verified",
        "api": "https://pmc.ncbi.nlm.nih.gov/tools/pmcaws/",
    },
    {
        "id": "pmc_bioc", "name": "PMC BioC API (NCBI)",
        "covers": "PMC open-access subset and author manuscripts, as passage-level JSON",
        "search": "no", "abstract": "no (use pubmed)", "fulltext": "text only (no PDF, no figures)", "supplement": "no",
        "key": "none", "key_env": [],
        "notes": "Accepts a PMID or PMCID.",
        "checked": "full text: OK",
        "api": "https://www.ncbi.nlm.nih.gov/research/bionlp/APIs/BioC-PMC/",
    },
    {
        "id": "biorxiv", "name": "bioRxiv / medRxiv",
        "covers": "life-science and medical preprints",
        "search": "no keyword search (by DOI or date only; use `preprints`)", "abstract": "yes",
        "fulltext": "PDF + JATS XML", "supplement": "no (web page only)", "key": "none", "key_env": [],
        "notes": "File downloads sit behind Cloudflare and may answer with a bot check from some networks.",
        "checked": "metadata, JATS XML: OK. PDF: bot check (HTTP 403) from the test network",
        "api": "https://api.biorxiv.org/",
    },
    {
        "id": "arxiv", "name": "arXiv",
        "covers": "preprints in q-bio, stat, cs (methods, machine learning for biology)",
        "search": "yes (field prefixes: all:, ti:, abs:, cat:q-bio.GN)", "abstract": "yes", "fulltext": "PDF",
        "supplement": "no", "key": "none", "key_env": [],
        "notes": "One request per 3 s; a shared institutional IP can be rate-limited (HTTP 429) by other users.",
        "checked": "search, PDF: OK (the API answered HTTP 429 for part of the day)",
        "api": "https://info.arxiv.org/help/api/",
    },
    {
        "id": "crossref", "name": "Crossref",
        "covers": "DOI metadata for almost every publisher",
        "search": "yes (bibliographic)", "abstract": "sometimes (only if the publisher deposited it)",
        "fulltext": "no (only links, used by the `publisher` step)", "supplement": "no",
        "key": "none", "key_env": [],
        "notes": "A contact e-mail moves you to the faster 'polite' pool.",
        "checked": "search, DOI lookup: OK",
        "api": "https://api.crossref.org/swagger-ui/",
    },
    {
        "id": "openalex", "name": "OpenAlex",
        "covers": "all scholarly works, with open-access locations",
        "search": "yes", "abstract": "yes (most works)", "fulltext": "link to an open-access PDF when one exists",
        "supplement": "no", "key": "optional", "key_env": ["OPENALEX_API_KEY"],
        "notes": "Without a key all users on your IP share one small daily budget; a free key has its own.",
        "checked": "search, DOI lookup: OK without a key",
        "api": "https://docs.openalex.org/",
    },
    {
        "id": "semanticscholar", "name": "Semantic Scholar",
        "covers": "all fields; citation graph",
        "search": "yes", "abstract": "yes (some publishers withhold it)",
        "fulltext": "link to an open-access PDF when one exists", "supplement": "no",
        "key": "optional", "key_env": ["S2_API_KEY"],
        "notes": "Keyless requests share one global pool; keyword search often returns HTTP 429 without a key.",
        "checked": "DOI lookup: OK without a key. Keyword search: HTTP 429 without a key",
        "api": "https://api.semanticscholar.org/api-docs/",
    },
    {
        "id": "unpaywall", "name": "Unpaywall",
        "covers": "legal open-access copies of any DOI (publisher, PMC, repositories)",
        "search": "no", "abstract": "no", "fulltext": "link to an open-access PDF when one exists",
        "supplement": "no", "key": "e-mail", "key_env": [],
        "notes": "No key, but a real contact e-mail is mandatory (placeholder addresses are rejected).",
        "checked": "refusal without a real e-mail: confirmed. Success path: not tested (no e-mail configured)",
        "api": "https://unpaywall.org/products/api",
    },
    {
        "id": "elsevier", "name": "Elsevier (ScienceDirect Article API)",
        "covers": "Elsevier journals (Cell Press, Lancet, ...): DOI prefix 10.1016",
        "search": "no", "abstract": "no",
        "fulltext": "XML; only if your institution is recognised by Elsevier (IP or institutional token)",
        "supplement": "no", "key": "required", "key_env": ["ELSEVIER_API_KEY", "ELSEVIER_INSTTOKEN"],
        "notes": "A free key is not enough by itself: without a link to a subscribing institution the API refuses (not_entitled).",
        "checked": "a key not linked to an institution: refused with HTTP 403 (not_entitled), even for an open-access article. Success path: not tested",
        "api": "https://dev.elsevier.com/documentation/ArticleRetrievalAPI.wadl",
    },
    {
        "id": "wiley", "name": "Wiley TDM API",
        "covers": "Wiley journals: DOI prefixes 10.1002, 10.1111",
        "search": "no", "abstract": "no", "fulltext": "PDF; subscribed content needs an institutional subscription",
        "supplement": "no", "key": "required", "key_env": ["WILEY_TDM_TOKEN"],
        "notes": "",
        "checked": "not tested (no token); without a token the API answers HTTP 400",
        "api": "https://onlinelibrary.wiley.com/library-info/resources/text-and-datamining",
    },
    {
        "id": "springer", "name": "Springer Nature Open Access API",
        "covers": "open-access articles of Springer, BMC and Nature Portfolio: DOI prefixes 10.1007, 10.1038, 10.1186",
        "search": "no", "abstract": "no", "fulltext": "JATS XML, open-access articles only",
        "supplement": "no", "key": "required", "key_env": ["SPRINGER_API_KEY"],
        "notes": "Subscription articles are not served by this API.",
        "checked": "HTTP 401 without a key: confirmed. Success path: not tested (no key)",
        "api": "https://dev.springernature.com/",
    },
    {
        "id": "publisher", "name": "Publisher site through the DOI (off by default)",
        "covers": "whatever your network is entitled to read (institutional subscription by IP)",
        "search": "no", "abstract": "no", "fulltext": "PDF named by the page (citation_pdf_url) or by Crossref links",
        "supplement": "no", "key": "none (subscription by IP)", "key_env": [],
        "notes": "Enable with allow_publisher_direct: true. Bot checks and login pages are logged, never bypassed.",
        "checked": "from a subscribing campus network: Nature and Genome Research PDFs OK; Science answered with a bot check (logged, not bypassed)",
        "api": "-",
    },
]

NOT_SUPPORTED: list[dict[str, str]] = [
    {"name": "Google Scholar", "reason": "no API; automated access is against its terms and is blocked"},
    {"name": "Web of Science, Scopus, Embase", "reason": "paid institutional APIs; index-only (no full text)"},
    {"name": "Sci-Hub, LibGen and similar", "reason": "copyright-infringing sources are excluded by design"},
    {"name": "Publisher supplementary files outside PMC", "reason": "no common API; they live on publisher "
     "pages behind bot checks. Supplements are fetched only from PMC (pmc_s3, europepmc)"},
    {"name": "Science (AAAS), AACR, OUP, NEJM, LWW full text", "reason": "no text-mining API; their sites "
     "answer scripts with a bot check. Open copies are still found through PMC / Unpaywall / OpenAlex"},
    {"name": "CORE", "reason": "needs a key and largely overlaps with Unpaywall + OpenAlex"},
]


def list_databases() -> dict[str, Any]:
    return {"verified": VERIFIED, "supported": DATABASES, "not_supported": NOT_SUPPORTED,
            "keys": {name: {**info, "set": bool(key(name))} for name, info in KEYS.items()}}


def markdown_table() -> str:
    """The three README tables (capabilities, keys, live check). `tests` keep the README in sync with this."""
    cap = ["| Database | Covers | Search | Abstract | Full text | Supplement | API key |", "|---|---|---|---|---|---|---|"]
    chk = [f"| Database | Result of the live check on {VERIFIED} |", "|---|---|"]
    for d in DATABASES:
        k = d["key"] + ("<br>" + "<br>".join(f"`{e}`" for e in d["key_env"]) if d["key_env"] else "")
        cap.append(f"| **{d['name']}**<br>`{d['id']}` | {d['covers']} | {d['search']} | {d['abstract']} | "
                   f"{d['fulltext']} | {d['supplement']} | {k} |")
        chk.append(f"| `{d['id']}` | {d['checked']} |")
    keys = ["| Environment variable | Used for | Where to get it |", "|---|---|---|"]
    keys += [f"| `{name}` | {info['for']} | {info['get']} |" for name, info in KEYS.items()]
    return "\n".join(cap) + "\n\n" + "\n".join(keys) + "\n\n" + "\n".join(chk)
