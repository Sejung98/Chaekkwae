"""Publisher full-text APIs (key required) and the opt-in publisher-site step.

- Elsevier Article Retrieval API: GET https://api.elsevier.com/content/article/doi/{doi}
  headers X-ELS-APIKey (+ X-ELS-Insttoken), Accept: text/xml. A key that is not tied to a subscribing
  institution gets the abstract only (open-access articles excepted).
- Wiley TDM API: GET https://api.wiley.com/onlinelibrary/tdm/v1/articles/{url-encoded doi}
  header Wiley-TDM-Client-Token; answers with the PDF.
- Springer Nature Open Access API: GET https://api.springernature.com/openaccess/jats?q=doi:{doi}&api_key=...
  JATS XML, open-access articles only (HTTP 401 without a key, checked 2026-11-20).
- Publisher site: follows https://doi.org/{doi} and reads the page's citation_pdf_url. This only works where
  the network has a subscription, and it never tries to get past a login page or a bot check.
"""

from __future__ import annotations

import re
from urllib.parse import quote, urljoin

from .catalog import key
from .core import Ctx
from .http import SourceError

ELSEVIER = "https://api.elsevier.com/content/article/doi"
WILEY = "https://api.wiley.com/onlinelibrary/tdm/v1/articles"
SPRINGER = "https://api.springernature.com/openaccess/jats"

PREFIXES = {
    "elsevier": ("10.1016/", "10.1006/", "10.1053/", "10.1067/", "10.1078/"),
    "wiley": ("10.1002/", "10.1111/", "10.1046/"),
    "springer": ("10.1007/", "10.1038/", "10.1186/"),
}
_PDF_META = re.compile(r'<meta[^>]{1,500}name=["\']citation_pdf_url["\'][^>]{1,500}content=["\']([^"\']{1,2000})["\']', re.I)
_PDF_META_REV = re.compile(r'<meta[^>]{1,500}content=["\']([^"\']{1,2000})["\'][^>]{1,500}name=["\']citation_pdf_url["\']', re.I)


def applies(publisher: str, doi: str | None) -> None:
    if not doi or not doi.startswith(PREFIXES[publisher]):
        raise SourceError("not_applicable", f"not a {publisher} DOI (prefixes {', '.join(PREFIXES[publisher])})")


def need_key(env: str) -> str:
    value = key(env)
    if not value:
        raise SourceError("no_key", f"environment variable {env} is not set (see list_databases -> keys)")
    return value


def elsevier_xml(ctx: Ctx, doi: str) -> bytes:
    headers = {"X-ELS-APIKey": need_key("ELSEVIER_API_KEY"), "Accept": "text/xml"}
    if key("ELSEVIER_INSTTOKEN"):
        headers["X-ELS-Insttoken"] = key("ELSEVIER_INSTTOKEN")
    try:
        return ctx.get(f"{ELSEVIER}/{doi}", headers=headers, auth=True).content
    except SourceError as exc:
        if exc.status == 403:  # the key is known but has no right to this article
            raise SourceError("not_entitled", exc.detail or "Elsevier refused: key is not entitled", 403, exc.url)
        raise


def elsevier_body(xml: bytes) -> str:
    """The article body inside an Elsevier response ('' when only metadata/abstract came back)."""
    m = re.search(rb"<originalText>(.*)</originalText>", xml, re.S)
    return m.group(1).decode("utf-8", "replace") if m else ""


def wiley_url(doi: str) -> str:
    return f"{WILEY}/{quote(doi, safe='')}"


def springer_article(ctx: Ctx, doi: str) -> bytes:
    xml = ctx.get(SPRINGER, params={"q": f"doi:{doi}", "api_key": need_key("SPRINGER_API_KEY")}, auth=True).content
    m = re.search(rb"<article[\s>].*</article>", xml, re.S)
    if not m:
        raise SourceError("not_open_access", "Springer Nature serves this DOI only to subscribers "
                                             "(the Open Access API has no record of it)")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + m.group(0)


def landing_pdf_urls(ctx: Ctx, doi: str) -> list[str]:
    """PDF URL advertised by the publisher's page for this DOI (citation_pdf_url), if any."""
    tmp = ctx.cfg.data_root / "_landing_page.tmp"   # streamed with a size cap: this is an arbitrary web page
    try:
        info = ctx.save(f"https://doi.org/{doi}", tmp, max_mb=5, retries=0)
        html = tmp.read_bytes()[:2_000_000].decode("utf-8", "replace")
    finally:
        tmp.unlink(missing_ok=True)
    m = _PDF_META.search(html) or _PDF_META_REV.search(html)
    return [urljoin(info["url"], m.group(1))] if m else []
