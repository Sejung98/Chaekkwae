# Changelog

## 0.1.0 (2026-11-20)

First release.

- `paperops` MCP server with 10 tools: `list_databases`, `check_environment`, `search_papers`, `get_paper`,
  `find_entity`, `fetch_fulltext`, `fetch_supplement`, `fetch_log`, `get_query`, `list_queries`.
- Search across PubMed, Europe PMC, preprints (Europe PMC `SRC:PPR`), PubTator3, arXiv, Crossref, OpenAlex and
  Semantic Scholar, merged by DOI / PMID. The query sent to each database and PubMed's interpretation are logged.
- Full-text ladder: PMC on AWS, Europe PMC, BioC, bioRxiv/medRxiv, arXiv, Unpaywall, OpenAlex, Semantic Scholar,
  Elsevier, Wiley, Springer Nature, and an opt-in publisher-site step. Files are validated (PDF opens, XML has a
  body, md5 for PMC files) before they count.
- Supplementary files from PMC on AWS and Europe PMC.
- Every attempt is stored with a reason code (`no_key`, `not_entitled`, `paywall`, `bot_wall`, `rate_limited`, ...).
- Subagents `literature-search` and `fulltext-fetch`, Orchestrator rules in `CLAUDE.md`, and a command line
  (`python -m paperops_mcp.cli`).
- Catalog checked against the live services on 2026-11-20; results are in the README.
