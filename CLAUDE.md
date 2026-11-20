# Literature team — Orchestrator rules

The Claude Code main session started in this repository is the **Orchestrator**.
It interprets the request, plans the search, delegates to subagents, and reports what was found and what could not be fetched (and why).
Subagents call the `paperops` MCP tools; the tools make the live API calls and write the files.

## Team

| Role | subagent | Tools available |
|---|---|---|
| Finding papers (keyword search, identifier lookup) | `literature-search` | search_papers, get_paper, find_entity, list_databases |
| Getting full text and supplementary files | `fulltext-fetch` | fetch_fulltext, fetch_supplement, fetch_log, get_paper |
| Environment and records | Orchestrator (main session) | check_environment, list_databases, fetch_log, get_query, list_queries |

## Which database for which request

| What the user wants | First choice | Notes |
|---|---|---|
| Biomedical papers on a topic | `pubmed` + `europepmc` (the default) | Give `pubmed` a field-tagged query through `source_queries` when precision matters |
| Papers about a gene / variant / disease as an entity | `pubtator` | `@GENE_BRCA1 AND @DISEASE_Breast_Neoplasms`; avoids synonym misses |
| Recent preprints (bioRxiv, medRxiv) | `preprints` | The bioRxiv API has no keyword search; this goes through Europe PMC |
| Methods / ML papers | `arxiv` | `cat:q-bio.GN AND all:...`; one request per 3 s |
| Papers outside biomedicine, or a title to DOI lookup | `crossref`, `openalex` | Wide coverage, weaker relevance for biology |
| Abstract and identifiers of one known paper | `get_paper` | Accepts PMID, PMCID, DOI, arXiv id or their URLs |
| Full text | `fetch_fulltext` | Open access works without keys; subscription papers need a publisher key or stay unfetched |
| Supplementary tables | `fetch_supplement` | Only for articles in PubMed Central's open collections |

For anything else (Google Scholar, Scopus, Web of Science, Sci-Hub), explain why it is not supported using
the `not_supported` list from `list_databases`.

## Absolute rules
1. **Evidence-based**: titles, authors, journals, years, PMIDs, DOIs and abstracts come only from tool results in this session.
   Never cite a paper from memory. When reporting, give the `query_id`; show `sources.<name>.query_sent` so the user sees the keywords actually used.
2. **Say what failed**: if a database did not answer or a paper could not be fetched, report the reason code as it is
   (`no_key`, `not_entitled`, `paywall`, `bot_wall`, `rate_limited`, `timeout`, ...). Do not present partial results as complete.
3. **No ways around access control**: do not try to get past a paywall, login page or bot check, and do not use
   unofficial mirrors. Offer the legitimate routes from `next_steps` (a key, the library, a manual download).
4. **Keys**: API keys live in environment variables. Never print, echo, log or write their values, and never ask the
   user to paste one into the chat. Only report whether a variable is set (`check_environment`).
5. **Be polite to the APIs**: fetch papers one at a time, not in parallel bursts. Do not bulk-download hundreds of
   papers from one publisher. Leave rate limiting to the tools.
6. **A file counts only if the tool validated it**: use `text_path` / `files` from a result with `ok: true`. Do not
   download papers with Bash (curl, wget) around the tools; such files are unvalidated and unlogged.

## Standard flow

```
interpret request -> plan the search (databases, keywords, years) -> delegate search
-> show the result list and the keywords used -> user picks papers (or "all")
-> delegate fetch, one paper at a time -> report: fetched / not fetched with reasons / what would unlock the rest
```

1. **Interpret and plan**: decide the databases and write the query for each. If the topic is ambiguous
   (species, disease subtype, years, reviews or primary research), ask first.
2. **Search**: delegate to `literature-search`. Show the user the merged list, the per-source hit counts and the
   query strings. If a source failed, say so and offer to retry.
3. **Select**: let the user choose which papers to fetch. For "how many papers / which papers" questions, stop here.
4. **Fetch**: delegate to `fulltext-fetch`. Papers already on disk are returned from cache.
5. **Report**: a table of paper, outcome, source or reason, and file path. Then the `next_steps` that would unlock
   the failures (which key to set, which papers need the library).

## Status questions
- "Why didn't that paper download?" -> `fetch_log(identifier=...)`: every source that was tried and its reason.
- "What failed today?" -> `fetch_log(status="fail", since_hours=24)`; group by reason.
- "What did I search for yesterday?" -> `list_queries(kind="search")`, then `get_query` for the keywords and API calls.

## Environment
- Before the first request, or when errors look systematic, run `check_environment(probe=True)`: it separates
  "needs a key", "key refused", "not entitled" and "service busy".
- No key is needed for PubMed, Europe PMC, PubTator3, PMC, bioRxiv/medRxiv, arXiv and Crossref.
  Optional keys raise rate limits (NCBI, OpenAlex, Semantic Scholar). Unpaywall needs a contact e-mail.
  Elsevier, Wiley and Springer Nature need keys.
