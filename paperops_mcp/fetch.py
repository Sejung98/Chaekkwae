"""Full-text and supplement retrieval: a ladder of sources, tried in order, each attempt logged.

Full-text ladder (first success stops it unless try_all=True):
  pmc_s3 -> europepmc -> pmc_bioc -> biorxiv -> arxiv -> unpaywall -> openalex -> semanticscholar
  -> elsevier -> wiley -> springer -> publisher (off by default)

Every step ends as ok / skip / fail with a reason from http.REASONS, stored in SQLite (`fetch_log`).
A file only counts when it passes validation: a PDF must open and an XML must contain the article body,
so a login page saved under a .pdf name is reported as `paywall`, not as success.
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import europepmc, http, ids, indexes, ncbi, papers, preprints, publishers, textio
from .core import Ctx
from .http import SourceError
from .state import new_query_id, now

SKIP_REASONS = {"not_applicable", "disabled", "no_key", "no_email"}
_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".svg"}


@dataclass
class Job:
    ctx: Ctx
    paper: dict[str, Any]
    dir: Path
    source: str = ""
    files: list[dict[str, Any]] = field(default_factory=list)
    texts: dict[str, str] = field(default_factory=dict)   # format -> extracted text
    notes: list[str] = field(default_factory=list)        # partial failures inside a step that succeeded

    def need(self, what: str) -> str:
        value = self.paper.get(what)
        if not value:
            raise SourceError("not_applicable", {
                "pmcid": "the paper has no PMCID (it is not in PubMed Central)",
                "doi": "the paper has no DOI",
                "arxiv": "not an arXiv paper",
            }[what])
        return value

    def dest(self, ext: str) -> Path:
        taken = {Path(f["path"]).name for f in self.files}
        name = f"fulltext.{ext}"
        return self.dir / (name if name not in taken else f"fulltext.{self.source}.{ext}")

    # ---- validated downloads
    def pdf(self, url: str, **kw: Any) -> dict[str, Any]:
        dest = self.dest("pdf")
        info = self.ctx.save(url, dest, **kw)
        chk = textio.pdf_info(dest, self.paper.get("title"))
        if not chk["ok"]:
            err = http.classify_not_a_file(dest)
            dest.unlink(missing_ok=True)
            if err.reason == "invalid_file":
                err.detail = chk["problem"]
            err.url = info["url"]
            raise err
        self.texts.setdefault("pdf", chk["text"])
        out = {"path": str(dest), "format": "pdf", "bytes": info["bytes"], "sha256": info["sha256"],
               "md5": info["md5"], "url": info["url"], "pages": chk["pages"], "text_chars": chk["chars"],
               "title_match": chk["title_match"]}
        if chk.get("warning"):
            out["warning"] = chk["warning"]
        return out

    def xml(self, url: str | None = None, data: bytes | None = None, **kw: Any) -> dict[str, Any]:
        dest = self.dest("xml")
        if data is None:
            info = self.ctx.save(url, dest, **kw)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            info = {"bytes": len(data), "sha256": None, "md5": None, "url": url}
        raw = dest.read_bytes()
        if not raw.lstrip().startswith(b"<") or b"<html" in raw[:3000].lower():
            err = http.classify_not_a_file(dest)
            dest.unlink(missing_ok=True)
            err.url = info["url"]
            raise err
        parsed = textio.jats_to_text(raw)
        if not parsed["has_body"]:
            dest.unlink(missing_ok=True)
            raise SourceError("abstract_only", f"the XML has no article body ({parsed['body_chars']} characters of body)",
                              url=info["url"])
        self.texts.setdefault("xml", parsed["text"])
        return {"path": str(dest), "format": "xml", "bytes": info["bytes"], "sha256": info["sha256"],
                "md5": info["md5"], "url": info["url"], "text_chars": len(parsed["text"])}

    def first_pdf(self, urls: list[str], limit: int = 3) -> list[dict[str, Any]]:
        last: SourceError | None = None
        for url in urls[:limit]:
            try:
                return [self.pdf(url)]
            except SourceError as exc:
                last = exc
        raise last or SourceError("not_open_access", "no PDF link")


def _check_md5(f: dict[str, Any], expected: str | None) -> None:
    if expected and f.get("md5") and f["md5"] != expected:
        Path(f["path"]).unlink(missing_ok=True)
        raise SourceError("invalid_file", f"md5 mismatch (expected {expected}, got {f['md5']})", url=f.get("url"))
    f["md5_verified"] = bool(expected and f.get("md5"))


# ---------------------------------------------------------------- full-text steps
def _pmc_s3(job: Job) -> list[dict]:
    pmcid = job.need("pmcid")
    listing = ncbi.s3_list(job.ctx, pmcid)
    prefix, md5s = listing["prefix"], ncbi.s3_md5(job.ctx, listing["prefix"])
    got, errors = [], []
    for ext, grab in (("xml", job.xml), ("pdf", job.pdf)):
        name = f"{prefix}.{ext}"
        if name not in listing["files"]:
            continue
        try:
            f = grab(ncbi.s3_url(prefix, name))
            _check_md5(f, md5s.get(name))
            got.append(f)
        except SourceError as exc:
            errors.append(exc)
            job.notes.append(f"{ext}: {exc.reason}")
    if not got:
        raise errors[0] if errors else SourceError("not_found", f"{prefix} has neither an XML nor a PDF file")
    return got


def _europepmc(job: Job) -> list[dict]:
    try:
        return [job.xml(europepmc.fulltext_url(job.need("pmcid")))]
    except SourceError as exc:
        if exc.reason == "not_found":
            raise SourceError("not_open_access", "not in the Europe PMC open-access subset", exc.status, exc.url)
        raise


def _pmc_bioc(job: Job) -> list[dict]:
    ident = job.need("pmcid")
    dest = job.dir / "fulltext.bioc.json"
    info = job.ctx.save(ncbi.bioc_url(ident), dest)
    parsed = textio.bioc_to_text(dest.read_bytes())
    if not parsed["ok"] or not parsed["has_body"]:
        dest.unlink(missing_ok=True)
        if not parsed["ok"]:
            raise SourceError("not_open_access", "no BioC record (not in the PMC open-access subset or the "
                                                 "author-manuscript collection)", url=info["url"])
        raise SourceError("abstract_only", "the BioC record has only title and abstract", url=info["url"])
    job.texts.setdefault("bioc", parsed["text"])
    return [{"path": str(dest), "format": "bioc_json", "bytes": info["bytes"], "sha256": info["sha256"],
             "url": info["url"], "text_chars": len(parsed["text"])}]


def _biorxiv(job: Job) -> list[dict]:
    doi = job.need("doi")
    if not ids.is_preprint_doi(doi):
        raise SourceError("not_applicable", "not a bioRxiv/medRxiv DOI")
    d = preprints.biorxiv_details(job.ctx, doi)
    got, errors = [], []
    for ext, grab, url in (("xml", job.xml, d.get("jats_url")), ("pdf", job.pdf, d.get("pdf_url"))):
        if not url:
            continue
        try:
            got.append(grab(url))
        except SourceError as exc:
            errors.append(exc)
            job.notes.append(f"{ext}: {exc.reason}")
    if not got:
        raise errors[-1] if errors else SourceError("not_found", "the bioRxiv API lists no files")
    return got


def _arxiv(job: Job) -> list[dict]:
    return [job.pdf(preprints.arxiv_pdf_url(job.need("arxiv")))]


def _unpaywall(job: Job) -> list[dict]:
    return job.first_pdf(indexes.unpaywall_pdf_urls(job.ctx, job.need("doi")))


def _from_index(job: Job, getter: Callable[[Ctx, dict], dict], name: str) -> list[dict]:
    if not (job.paper.get("doi") or job.paper.get("pmid")):
        raise SourceError("not_applicable", "needs a DOI or PMID")
    urls = getter(job.ctx, job.paper).get("oa_pdf_urls") or []
    if not urls:
        raise SourceError("not_open_access", f"{name} lists no open-access PDF for this paper")
    return job.first_pdf(urls)


def _openalex(job: Job) -> list[dict]:
    return _from_index(job, indexes.openalex_get, "OpenAlex")


def _semanticscholar(job: Job) -> list[dict]:
    return _from_index(job, indexes.s2_get, "Semantic Scholar")


def _elsevier(job: Job) -> list[dict]:
    doi = job.need("doi")
    publishers.applies("elsevier", doi)
    xml = publishers.elsevier_xml(job.ctx, doi)
    body = textio.strip_tags(publishers.elsevier_body(xml))
    if len(body) < 2 * textio.MIN_BODY_CHARS:
        raise SourceError("not_entitled", "Elsevier returned metadata/abstract only: the key is valid but is not "
                                          "linked to a subscribing institution (ask the library for ELSEVIER_INSTTOKEN)")
    dest = job.dest("xml")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(xml)
    job.texts.setdefault("xml", body)
    return [{"path": str(dest), "format": "xml", "bytes": len(xml), "url": f"{publishers.ELSEVIER}/{doi}",
             "text_chars": len(body)}]


def _wiley(job: Job) -> list[dict]:
    doi = job.need("doi")
    publishers.applies("wiley", doi)
    token = publishers.need_key("WILEY_TDM_TOKEN")
    try:
        return [job.pdf(publishers.wiley_url(doi), headers={"Wiley-TDM-Client-Token": token}, auth=True)]
    except SourceError as exc:
        if exc.status == 403:
            raise SourceError("not_entitled", "Wiley refused: the token's institution has no access to this article",
                              403, exc.url)
        raise


def _springer(job: Job) -> list[dict]:
    doi = job.need("doi")
    publishers.applies("springer", doi)
    return [job.xml(url=f"{publishers.SPRINGER}?q=doi:{doi}", data=publishers.springer_article(job.ctx, doi))]


def _publisher(job: Job) -> list[dict]:
    if not job.ctx.cfg.get("allow_publisher_direct"):
        raise SourceError("disabled", "allow_publisher_direct is false in config.yaml (turn it on only on a "
                                      "network with a subscription)")
    doi = job.need("doi")
    urls: list[str] = []
    try:
        links = indexes.crossref_get(job.ctx, doi).get("links") or []
        urls += [l["url"] for l in links if l.get("url") and (l.get("content_type") == "application/pdf"
                                                              or l["url"].lower().endswith(".pdf"))
                 and not l["url"].startswith(("https://api.elsevier.com", "https://api.wiley.com"))]
    except SourceError:
        pass
    urls += publishers.landing_pdf_urls(job.ctx, doi)
    urls = list(dict.fromkeys(urls))
    if not urls:
        raise SourceError("paywall", "the publisher page names no PDF (citation_pdf_url) and Crossref has no PDF link")
    return job.first_pdf(urls)


FULLTEXT_LADDER: list[tuple[str, Callable[[Job], list[dict]]]] = [
    ("pmc_s3", _pmc_s3), ("europepmc", _europepmc), ("pmc_bioc", _pmc_bioc), ("biorxiv", _biorxiv),
    ("arxiv", _arxiv), ("unpaywall", _unpaywall), ("openalex", _openalex), ("semanticscholar", _semanticscholar),
    ("elsevier", _elsevier), ("wiley", _wiley), ("springer", _springer), ("publisher", _publisher),
]


# ---------------------------------------------------------------- supplement steps
def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", Path(name.replace("\\", "/")).name).strip(".")[:150] or "file"


def _supp_pmc_s3(job: Job) -> list[dict]:
    pmcid = job.need("pmcid")
    listing = ncbi.s3_list(job.ctx, pmcid)
    prefix, names = listing["prefix"], listing["files"]
    main = {f"{prefix}.{e}" for e in ("xml", "pdf", "txt", "json")}
    wanted: list[str] = []
    if f"{prefix}.xml" in names:
        hrefs = textio.jats_to_text(job.ctx.get(ncbi.s3_url(prefix, f"{prefix}.xml")).content)["supplement_hrefs"]
        wanted = [n for n in names if n in {Path(h).name for h in hrefs}]
    if not wanted:  # the XML names no supplement: fall back to non-figure files
        wanted = [n for n in names if n not in main and Path(n).suffix.lower() not in _IMAGE_EXT]
    if not wanted:
        raise SourceError("no_supplement", f"{prefix} has no supplementary files in the PMC bucket")
    md5s = ncbi.s3_md5(job.ctx, prefix)
    limit = int(job.ctx.cfg.get("max_supplement_files", 50))
    got = []
    for name in sorted(wanted)[:limit]:
        info = job.ctx.save(ncbi.s3_url(prefix, name), job.dir / "supplement" / _safe_name(name))
        f = {"path": info["path"], "format": Path(name).suffix.lstrip(".").lower(), "bytes": info["bytes"],
             "sha256": info["sha256"], "md5": info["md5"], "url": info["url"]}
        _check_md5(f, md5s.get(name))
        got.append(f)
    if len(wanted) > limit:
        got.append({"note": f"{len(wanted) - limit} more files not downloaded (max_supplement_files={limit})"})
    return got


def _supp_europepmc(job: Job) -> list[dict]:
    pmcid = job.need("pmcid")
    zpath = job.dir / "supplement" / "_europepmc.zip"
    try:
        info = job.ctx.save(europepmc.supplement_url(pmcid), zpath)
    except SourceError as exc:
        if exc.reason == "not_found":
            raise SourceError("no_supplement", "Europe PMC has no supplementary files for this article "
                                               "(none exist, or it is outside the open-access subset)", exc.status, exc.url)
        raise
    got, total, cap = [], 0, int(float(job.ctx.cfg.get("max_download_mb", 200)) * 1024 * 1024)
    try:
        with zipfile.ZipFile(zpath) as zf:
            for member in zf.infolist():
                if member.is_dir():
                    continue
                total += member.file_size
                if total > cap:
                    raise SourceError("too_large", "unpacked supplement exceeds max_download_mb", url=info["url"])
                dest = zpath.parent / _safe_name(member.filename)
                if any(f["path"] == str(dest) for f in got):   # same base name in two folders of the archive
                    dest = dest.with_name(f"{len(got)}_{dest.name}")
                dest.write_bytes(zf.read(member))
                got.append({"path": str(dest), "format": dest.suffix.lstrip(".").lower(), "bytes": member.file_size,
                            "url": info["url"]})
    except zipfile.BadZipFile:
        raise SourceError("invalid_file", "Europe PMC did not return a zip archive", url=info["url"])
    finally:
        zpath.unlink(missing_ok=True)
    if not got:
        raise SourceError("no_supplement", "the Europe PMC archive is empty", url=info["url"])
    return got


SUPPLEMENT_LADDER: list[tuple[str, Callable[[Job], list[dict]]]] = [
    ("pmc_s3", _supp_pmc_s3), ("europepmc", _supp_europepmc),
]


# ---------------------------------------------------------------- runner
def _run_ladder(job: Job, kind: str, ladder: list, query_id: str, sources: list[str] | None,
                done: Callable[[], bool], try_all: bool) -> list[dict[str, Any]]:
    attempts = []
    for name, step in ladder:
        if sources and name not in sources:
            continue
        if done() and not try_all:
            break
        job.source, job.notes = name, []
        a: dict[str, Any] = {"source": name}
        try:
            files = step(job)
            job.files.extend(f for f in files if f.get("path"))
            a.update(status="ok", reason="ok", files=files,
                     url=next((f.get("url") for f in files if f.get("url")), None))
            if job.notes:
                a["detail"] = "got " + ", ".join(f["format"] for f in files if f.get("format"))                     + "; not delivered here -> " + ", ".join(job.notes)
        except SourceError as exc:
            a.update(status="skip" if exc.reason in SKIP_REASONS else "fail", reason=exc.reason,
                     detail=exc.detail[:300], url=exc.url, http_status=exc.status)
        except Exception as exc:  # a bug in one step must not hide the result of the others
            a.update(status="fail", reason="internal_error", detail=f"{type(exc).__name__}: {exc}"[:300])
        job.ctx.db.add_attempt(query_id, job.paper["key"], kind, a)
        attempts.append(a)
    return attempts


def _next_steps(paper: dict, attempts: list[dict]) -> list[str]:
    reasons = {a["reason"] for a in attempts}
    tips = []
    for a in attempts:
        if a["reason"] == "no_key":
            tips.append(f"{a['source']}: {a['detail']}")
    if "no_email" in reasons:
        tips.append("Set contact_email in config.yaml (or PAPEROPS_EMAIL) to enable Unpaywall.")
    if reasons & {"rate_limited", "timeout", "server_error", "network_error"}:
        tips.append("Some sources were busy or unreachable; retrying later may succeed.")
    if "not_entitled" in reasons:
        tips.append("A publisher key works but your institution is not linked to it; ask the library.")
    if reasons & {"paywall", "bot_wall", "not_open_access"} and paper.get("doi"):
        tips.append(f"No free copy was found. With a subscription, open https://doi.org/{paper['doi']} in a browser "
                    "(or run on the campus network with allow_publisher_direct: true).")
    return tips


def _paper_dir(ctx: Ctx, paper: dict) -> Path:
    return ctx.cfg.data_root / "papers" / paper["key"]


def fetch_fulltext(ctx: Ctx, identifier: str, want_pdf: bool = False, sources: list[str] | None = None,
                   try_all: bool = False, force: bool = False) -> dict[str, Any]:
    params = {"identifier": identifier, "want_pdf": want_pdf, "sources": sources, "try_all": try_all}
    unknown = sorted(set(sources or []) - {n for n, _ in FULLTEXT_LADDER})
    if unknown:
        raise ValueError(f"unknown sources {unknown}; valid: {[n for n, _ in FULLTEXT_LADDER]}")
    paper = papers.resolve(ctx, identifier, need_abstract=False)
    pdir = _paper_dir(ctx, paper)
    meta_path, text_path = pdir / "metadata.json", pdir / "fulltext.txt"
    if not force and not try_all and not sources and meta_path.exists() and text_path.exists():
        old = json.loads(meta_path.read_text(encoding="utf-8"))
        same = all((old.get("paper") or {}).get(k) == paper.get(k) for k in ("doi", "pmid"))  # folder names can collide
        if same and (not want_pdf or any(f.get("format") == "pdf" for f in old.get("files", []))):
            return {"ok": True, "cached": True, "paper": old.get("paper"), "files": old.get("files"),
                    "text_path": str(text_path), "query_id": old.get("query_id"),
                    "note": "already fetched; pass force=True to download again"}

    job = Job(ctx, paper, pdir)
    qid = new_query_id("fulltext")

    def done() -> bool:
        return bool(job.texts) and (not want_pdf or "pdf" in job.texts)

    attempts = _run_ladder(job, "fulltext", FULLTEXT_LADDER, qid, sources, done, try_all)
    ok = bool(job.texts)
    brief = {k: paper.get(k) for k in ("key", "pmid", "pmcid", "doi", "arxiv", "title", "journal", "year", "is_preprint")}
    out: dict[str, Any] = {"ok": ok, "query_id": qid, "paper": brief, "files": job.files, "attempts": attempts}
    if ok:
        best = next(f for f in ("xml", "bioc", "pdf") if f in job.texts)
        pdir.mkdir(parents=True, exist_ok=True)
        text_path.write_text(job.texts[best], encoding="utf-8")
        out.update(text_path=str(text_path), text_from=best, text_chars=len(job.texts[best]),
                   got_from=[a["source"] for a in attempts if a["status"] == "ok"])
        if want_pdf and "pdf" not in job.texts:
            out["note"] = "text retrieved, but no source delivered a PDF"
            out["next_steps"] = _next_steps(paper, attempts)
        meta_path.write_text(json.dumps({"paper": {**brief, "authors": paper.get("authors"),
                                                   "abstract": paper.get("abstract")},
                                         "files": job.files, "query_id": qid, "fetched_at": now()},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        out["why"] = [f"{a['source']}: {a['reason']}" + (f" ({a['detail']})" if a.get("detail") else "")
                      for a in attempts]
        out["next_steps"] = _next_steps(paper, attempts)
    summary = {"ok": ok, "paper_key": paper["key"], "title": paper.get("title"),
               "outcome": {a["source"]: a["reason"] for a in attempts}}
    ctx.db.save_query("fulltext", params, summary, ctx.log.calls, str(text_path) if ok else None, query_id=qid)
    out["api_queries"] = ctx.log.calls
    return out


def fetch_supplement(ctx: Ctx, identifier: str, sources: list[str] | None = None) -> dict[str, Any]:
    paper = papers.resolve(ctx, identifier, need_abstract=False)
    job = Job(ctx, paper, _paper_dir(ctx, paper))
    qid = new_query_id("supplement")
    attempts = _run_ladder(job, "supplement", SUPPLEMENT_LADDER, qid, sources, lambda: bool(job.files), False)
    ok = bool(job.files)
    out: dict[str, Any] = {"ok": ok, "query_id": qid,
                           "paper": {k: paper.get(k) for k in ("key", "pmid", "pmcid", "doi", "title")},
                           "files": job.files, "attempts": attempts}
    if ok:
        out["dir"] = str(job.dir / "supplement")
    else:
        out["why"] = [f"{a['source']}: {a['reason']}" + (f" ({a['detail']})" if a.get("detail") else "")
                      for a in attempts]
        out["next_steps"] = ["Supplementary files can only be fetched for articles in PubMed Central's open "
                             "collections. Otherwise download them from the publisher page"
                             + (f": https://doi.org/{paper['doi']}" if paper.get("doi") else ".")]
    ctx.db.save_query("supplement", {"identifier": identifier, "sources": sources},
                      {"ok": ok, "paper_key": paper["key"], "n_files": len(job.files),
                       "outcome": {a["source"]: a["reason"] for a in attempts}}, ctx.log.calls,
                      str(job.dir / "supplement") if ok else None, query_id=qid)
    out["api_queries"] = ctx.log.calls
    return out


def fetch_log(ctx: Ctx, identifier: str | None = None, status: str | None = None, reason: str | None = None,
              since_hours: float | None = None, limit: int = 100) -> dict[str, Any]:
    """Past attempts, newest first, with a count per reason. `identifier` may be any ID of the paper."""
    paper_key = None
    if identifier:
        paper_key = identifier if identifier.startswith(("doi_", "pmid_", "arxiv_")) else \
            papers.resolve(ctx, identifier, need_abstract=False)["key"]
    rows = ctx.db.attempts(paper_key, status, reason, since_hours, limit)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["reason"]] = counts.get(r["reason"], 0) + 1
    return {"paper_key": paper_key, "n": len(rows), "by_reason": counts,
            "reason_meanings": {r: http.REASONS.get(r, "") for r in counts}, "attempts": rows}
