"""Checking that a downloaded file is really a readable paper, and turning it into plain text.

- PDF: must start with %PDF and open with pypdf; text is extracted page by page.
- JATS XML (PMC, Europe PMC, bioRxiv, Springer): must contain an article <body>.
- BioC JSON (NCBI): must contain passages beyond title and abstract.
"""

from __future__ import annotations

import html
import json
import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .ids import norm_title

logging.getLogger("pypdf").setLevel(logging.ERROR)  # font-encoding warnings are noise here

_TAG = re.compile(r"<[^>]+>")
MIN_BODY_CHARS = 1500  # shorter than this is an abstract, not an article


def squash(text: str) -> str:
    return " ".join(text.split())


def strip_tags(markup: str | None) -> str:
    """Plain text from a small HTML/JATS fragment (titles, Crossref abstracts)."""
    return squash(html.unescape(_TAG.sub(" ", markup or "")))


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _text(el: ET.Element) -> str:
    return squash("".join(el.itertext()))


def _walk(el: ET.Element, depth: int, out: list[str]) -> None:
    for ch in el:
        tag = _local(ch.tag)
        if tag == "sec":
            title = next((c for c in ch if _local(c.tag) == "title"), None)
            if title is not None and _text(title):
                out.append("#" * min(depth + 2, 6) + " " + _text(title))
            _walk(ch, depth + 1, out)
        elif tag in ("title", "label"):
            continue
        elif tag == "p":
            if _text(ch):
                out.append(_text(ch))
        elif tag in ("fig", "table-wrap", "boxed-text", "supplementary-material"):
            label = next((_text(c) for c in ch.iter() if _local(c.tag) == "label"), "")
            caption = next((_text(c) for c in ch.iter() if _local(c.tag) == "caption"), "")
            if label or caption:
                out.append(f"[{label or tag}] {caption}".strip())
            for row in (r for r in ch.iter() if _local(r.tag) == "tr"):
                cells = [_text(c) for c in row if _local(c.tag) in ("td", "th")]
                if any(cells):
                    out.append(" | ".join(cells))
        elif tag == "list":
            out.extend("- " + _text(item) for item in ch if _local(item.tag) == "list-item" and _text(item))
        else:
            _walk(ch, depth, out)


def jats_to_text(data: bytes) -> dict[str, Any]:
    """Parse JATS XML. Returns {ok, has_body, body_chars, title, text, supplement_hrefs}."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        # Undefined entities or a truncated file: fall back to tag stripping so the text is still usable.
        raw = data.decode("utf-8", "replace")
        m = re.search(r"<body[\s>].*?</body>", raw, re.S)
        body = strip_tags(m.group(0)) if m else ""
        return {"ok": bool(m), "has_body": len(body) >= MIN_BODY_CHARS, "body_chars": len(body), "title": None,
                "text": strip_tags(raw), "supplement_hrefs": [], "parse_warning": str(exc)}
    article = root if _local(root.tag) == "article" else next(
        (e for e in root.iter() if _local(e.tag) == "article"), root)
    title = next((_text(e) for e in article.iter() if _local(e.tag) == "article-title"), None)
    out: list[str] = [f"# {title}"] if title else []
    for ab in (e for e in article.iter() if _local(e.tag) == "abstract"):
        out.append("## Abstract")
        before = len(out)
        _walk(ab, 1, out)
        if len(out) == before and _text(ab):
            out.append(_text(ab))
        break
    body = next((e for e in article.iter() if _local(e.tag) == "body"), None)
    body_lines: list[str] = []
    if body is not None:
        _walk(body, 0, body_lines)
    body_chars = sum(len(x) for x in body_lines)
    hrefs = []
    for sm in (e for e in article.iter() if _local(e.tag) == "supplementary-material"):
        for e in sm.iter():
            hrefs.extend(v for k, v in e.attrib.items() if _local(k) == "href" and v)
    return {"ok": True, "has_body": body_chars >= MIN_BODY_CHARS, "body_chars": body_chars, "title": title,
            "text": "\n\n".join(out + body_lines), "supplement_hrefs": sorted(set(hrefs))}


def bioc_to_text(data: bytes) -> dict[str, Any]:
    """Parse NCBI BioC JSON (PMC OA API). Returns {ok, has_body, body_chars, text}."""
    try:
        doc = json.loads(data)
        coll = doc[0] if isinstance(doc, list) else doc
        passages = coll["documents"][0]["passages"]
    except (ValueError, KeyError, IndexError, TypeError):
        return {"ok": False, "has_body": False, "body_chars": 0, "text": ""}
    lines, body_chars = [], 0
    for p in passages:
        text = squash(p.get("text") or "")
        if not text:
            continue
        infons = p.get("infons") or {}
        section, kind = (infons.get("section_type") or "").upper(), (infons.get("type") or "").lower()
        if section == "REF":
            continue
        if section not in ("TITLE", "ABSTRACT"):
            body_chars += len(text)
        if kind == "front":
            lines.append(f"# {text}")
        elif kind.startswith("title"):
            lines.append(f"## {text}")
        else:
            lines.append(text)
    return {"ok": True, "has_body": body_chars >= MIN_BODY_CHARS, "body_chars": body_chars,
            "text": "\n\n".join(lines)}


def pdf_info(path: Path, title: str | None = None, max_pages: int = 400) -> dict[str, Any]:
    """Open a PDF and extract its text. Returns {ok, pages, chars, text, title_match, problem}."""
    with open(path, "rb") as fh:
        if b"%PDF-" not in fh.read(1024):
            return {"ok": False, "problem": "does not start with %PDF"}
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        if reader.is_encrypted:
            reader.decrypt("")
        n = len(reader.pages)
        texts = [(reader.pages[i].extract_text() or "") for i in range(min(n, max_pages))]
    except Exception as exc:  # pypdf raises many types on damaged files
        return {"ok": False, "problem": f"cannot be opened as PDF: {type(exc).__name__}: {exc}"[:200]}
    if n == 0:
        return {"ok": False, "problem": "PDF has no pages"}
    text = "\n\n".join(t.strip() for t in texts)
    out: dict[str, Any] = {"ok": True, "pages": n, "chars": len(text), "text": text, "title_match": None}
    if len(text) < 200 * min(n, 3):
        out["warning"] = "almost no extractable text (scanned images?); the PDF itself is valid"
    words = [w for w in norm_title(title).split() if len(w) >= 4]
    if words and not out.get("warning"):
        front = norm_title(" ".join(texts[:2]))
        hits = sum(1 for w in words if w in front)
        out["title_match"] = hits / len(words) >= 0.6
        if not out["title_match"]:
            out["warning"] = "the paper's title was not found on the first pages; check that this is the right PDF"
    return out
