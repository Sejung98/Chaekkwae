"""Command line for the same functions the MCP server exposes (handy for testing and for other agents).

  python -m paperops_mcp.cli databases [--markdown]
  python -m paperops_mcp.cli check [--probe] [--table] [--db pubmed ...]
  python -m paperops_mcp.cli search "mutational signature breast cancer" [--sources pubmed arxiv] [-n 10]
  python -m paperops_mcp.cli entity "BRAF V600E"
  python -m paperops_mcp.cli paper 32025018
  python -m paperops_mcp.cli fetch 10.1038/s41586-020-1943-3 [--pdf] [--try-all] [--force]
  python -m paperops_mcp.cli supplement PMC7054213
  python -m paperops_mcp.cli log [--id ID] [--status fail] [--reason paywall]
"""

from __future__ import annotations

import argparse
import json
import sys

from . import catalog, environment, fetch, ncbi, papers, search
from .core import open_ctx
from .http import REASONS, SourceError


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="paperops", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("databases")
    p.add_argument("--markdown", action="store_true")
    p = sub.add_parser("check")
    p.add_argument("--probe", action="store_true")
    p.add_argument("--table", action="store_true", help="one line per database instead of JSON")
    p.add_argument("--db", nargs="*")
    p = sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("--sources", nargs="*")
    p.add_argument("-n", "--max-results", type=int, default=20)
    p.add_argument("--year-from", type=int)
    p.add_argument("--year-to", type=int)
    p.add_argument("--sort", default="relevance", choices=["relevance", "date"])
    p = sub.add_parser("entity")
    p.add_argument("text")
    p = sub.add_parser("paper")
    p.add_argument("identifier")
    p = sub.add_parser("fetch")
    p.add_argument("identifier")
    p.add_argument("--pdf", action="store_true")
    p.add_argument("--sources", nargs="*")
    p.add_argument("--try-all", action="store_true")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("supplement")
    p.add_argument("identifier")
    p = sub.add_parser("log")
    p.add_argument("--id")
    p.add_argument("--status")
    p.add_argument("--reason")
    p.add_argument("--since-hours", type=float)
    p.add_argument("--limit", type=int, default=100)
    a = ap.parse_args(argv)

    if a.cmd == "databases":
        print(catalog.markdown_table() if a.markdown else json.dumps(catalog.list_databases(), indent=2))
        return 0
    try:
        with open_ctx() as ctx:
            if a.cmd == "check":
                out = environment.check_environment(ctx, a.probe, a.db)
            elif a.cmd == "search":
                out = search.search_papers(ctx, a.query, a.sources, a.max_results, a.year_from, a.year_to, a.sort)
            elif a.cmd == "entity":
                out = {"entities": ncbi.pubtator_entities(ctx, a.text)}
            elif a.cmd == "paper":
                out = papers.get_paper(ctx, a.identifier)
            elif a.cmd == "fetch":
                out = fetch.fetch_fulltext(ctx, a.identifier, a.pdf, a.sources, a.try_all, a.force)
            elif a.cmd == "supplement":
                out = fetch.fetch_supplement(ctx, a.identifier)
            else:
                out = fetch.fetch_log(ctx, a.id, a.status, a.reason, a.since_hours, a.limit)
    except SourceError as exc:
        out = {"ok": False, "reason": exc.reason, "meaning": REASONS.get(exc.reason, ""), "error": exc.detail}
    except ValueError as exc:
        out = {"ok": False, "error": str(exc)}
    sys.stdout.reconfigure(encoding="utf-8")
    if a.cmd == "check" and a.table:
        for r in out["databases"]:
            print(f"  {r['id']:16} {r['status']:12} {r.get('reason', ''):15} {r.get('detail', '')[:80]}")
        return 0
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
