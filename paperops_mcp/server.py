"""paperops MCP server (stdio).

Run: python -m paperops_mcp.server
In the `mcp` package 2.x, FastMCP was renamed to MCPServer, so both are supported.
"""

import os
import traceback
from typing import Callable, Optional

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
from mcp.types import ToolAnnotations

from . import catalog, environment, fetch, ncbi, papers, search
from .core import Ctx, open_ctx
from .http import REASONS, SourceError

INSTRUCTIONS = """\
Literature tools for bioinformatics. Rules:
1) Titles, authors, PMIDs, DOIs and abstracts must come only from these tools' results. Never write a citation from memory.
2) Every result has a query_id and api_queries (the API calls it is based on). Cite the query_id when reporting.
3) When a fetch fails, report the reason code of each attempted source as it is (no_key, paywall, bot_wall, ...).
   Do not look for ways around a paywall or a bot check.
4) API keys live in environment variables. Never print, echo or write their values.
"""

server = _Server(name="paperops", instructions=INSTRUCTIONS)

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
LOCAL_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)


def _run(fn: Callable[[Ctx], dict]) -> dict:
    try:
        with open_ctx() as ctx:
            out = fn(ctx)
        out.setdefault("ok", True)
        return out
    except SourceError as exc:
        return {"ok": False, "reason": exc.reason, "meaning": REASONS.get(exc.reason, ""), "error": exc.detail}
    except Exception as exc:  # tool errors are returned in structured form
        return {"ok": False, "error_type": type(exc).__name__, "error": str(exc),
                "trace": traceback.format_exc(limit=3)[-1500:] if os.environ.get("PAPEROPS_DEBUG") else None}


@server.tool(annotations=READ_ONLY)
def list_databases() -> dict:
    """Catalog of the literature databases: what each gives (search / abstract / full text / supplement),
    whether it needs an API key and which environment variable holds it, plus what is not supported and why.
    Contains no paper counts."""
    return {"ok": True, **catalog.list_databases()}


@server.tool(annotations=READ_ONLY)
def check_environment(probe: bool = False, databases: Optional[list[str]] = None) -> dict:
    """Which API keys are set (never their values) and the config summary. With probe=True, also calls every
    database once and reports ok / needs_key / needs_email / off / fail with a reason, so 'no key' can be told
    apart from 'key refused' and 'service down'. A full probe takes up to a few minutes when a service is slow;
    pass databases=['pubmed', ...] to probe only some."""
    return _run(lambda ctx: environment.check_environment(ctx, probe, databases))


@server.tool(annotations=LOCAL_WRITE)
def search_papers(
    query: str,
    sources: Optional[list[str]] = None,
    max_results: int = 20,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    sort: str = "relevance",
    source_queries: Optional[dict[str, str]] = None,
) -> dict:
    """Keyword search (live APIs), merged and de-duplicated across databases.
    sources: any of pubmed, europepmc (default: these two), preprints, pubtator, arxiv, crossref, openalex,
    semanticscholar. sort: 'relevance' | 'date'. The same query string goes to every source unless
    source_queries gives a source its own, e.g. {"pubmed": "BRCA1[tiab] AND breast neoplasms[mesh]",
    "pubtator": "@GENE_BRCA1 AND @DISEASE_Breast_Neoplasms", "arxiv": "cat:q-bio.GN AND all:signature"}.
    Returns per-source status (query_sent, total hits, PubMed's interpretation, or the failure reason),
    compact results with abstracts cut to 500 characters, and result_path with the full records."""
    return _run(lambda ctx: search.search_papers(ctx, query, sources, max_results, year_from, year_to, sort,
                                                 source_queries))


@server.tool(annotations=READ_ONLY)
def find_entity(text: str) -> dict:
    """Look up PubTator3 entity ids for a gene, variant, disease or chemical name (e.g. 'BRAF V600E' ->
    @VARIANT_p.V600E_BRAF_human). Use the ids in a pubtator query: search_papers(..., sources=['pubtator'],
    source_queries={'pubtator': '@GENE_BRCA1 AND @DISEASE_Breast_Neoplasms'})."""
    return _run(lambda ctx: {"entities": ncbi.pubtator_entities(ctx, text), "api_queries": ctx.log.calls})


@server.tool(annotations=READ_ONLY)
def get_paper(identifier: str) -> dict:
    """Resolve one paper from a PMID, PMCID, DOI, arXiv id or their URL: all identifiers, title, authors,
    journal, year, full abstract (with the database it came from), MeSH terms. `lookup` lists every database
    that was asked and what it answered."""
    return _run(lambda ctx: papers.get_paper(ctx, identifier))


@server.tool(annotations=LOCAL_WRITE)
def fetch_fulltext(
    identifier: str,
    want_pdf: bool = False,
    sources: Optional[list[str]] = None,
    try_all: bool = False,
    force: bool = False,
) -> dict:
    """Download and validate the full text of one paper into data_root/papers/<key>/ and write fulltext.txt.
    Tries, in order: pmc_s3, europepmc, pmc_bioc, biorxiv, arxiv, unpaywall, openalex, semanticscholar,
    elsevier, wiley, springer, publisher. Stops at the first success (want_pdf=True keeps going until a PDF is
    saved; try_all=True runs every source for diagnosis; sources=[...] restricts the ladder).
    Returns files (path, format, size, sha256), text_path, and `attempts`: one entry per source with
    status ok/skip/fail and a reason code. On failure `why` and `next_steps` explain what would help."""
    return _run(lambda ctx: fetch.fetch_fulltext(ctx, identifier, want_pdf, sources, try_all, force))


@server.tool(annotations=LOCAL_WRITE)
def fetch_supplement(identifier: str, sources: Optional[list[str]] = None) -> dict:
    """Download the supplementary files of one paper into data_root/papers/<key>/supplement/.
    Works for articles in PubMed Central's open collections (sources: pmc_s3, then europepmc).
    Publisher-hosted supplements of other articles are not retrievable; the result says so."""
    return _run(lambda ctx: fetch.fetch_supplement(ctx, identifier, sources))


@server.tool(annotations=READ_ONLY)
def fetch_log(
    identifier: Optional[str] = None,
    status: Optional[str] = None,
    reason: Optional[str] = None,
    since_hours: Optional[float] = None,
    limit: int = 100,
) -> dict:
    """Past fetch attempts from the local log, newest first, with a count per reason code and what each code
    means. Filter by paper (any identifier), status (ok|skip|fail), reason (e.g. paywall, no_key), or age."""
    return _run(lambda ctx: fetch.fetch_log(ctx, identifier, status, reason, since_hours, limit))


@server.tool(annotations=READ_ONLY)
def get_query(query_id: str) -> dict:
    """A stored search or fetch: the parameters (keywords, filters), the summary, the exact API calls and,
    for fetches, every attempt."""

    def go(ctx: Ctx) -> dict:
        q = ctx.db.get_query(query_id)
        return q if q else {"ok": False, "error": f"no query {query_id}"}

    return _run(go)


@server.tool(annotations=READ_ONLY)
def list_queries(kind: Optional[str] = None, limit: int = 20) -> dict:
    """Recent searches and fetches (kind: search | paper | fulltext | supplement), newest first."""
    return _run(lambda ctx: {"queries": ctx.db.list_queries(kind, limit)})


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
