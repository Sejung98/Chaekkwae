"""Shared HTTP layer: polite rate limiting, retries, failure reasons, downloads, and an API call log.

Every failure is turned into a `SourceError` with a short machine-readable `reason`
(see REASONS). That reason is what ends up in the fetch log, so a user can tell
"no API key" from "paywall" from "the server was slow".

Secrets never reach the log: query parameters such as api_key / email are masked, and
request headers (where most keys travel) are not logged at all.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import socket
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from . import __version__
from .catalog import KEYS

REASONS: dict[str, str] = {
    "ok": "retrieved and validated",
    "not_applicable": "this source does not handle this kind of identifier (e.g. the paper has no PMCID)",
    "no_key": "the source needs an API key and the environment variable is not set",
    "no_email": "the source needs a contact e-mail (config contact_email or PAPEROPS_EMAIL)",
    "key_rejected": "the source refused the API key (invalid, expired or not activated)",
    "not_entitled": "the key works but the account/institution has no right to this full text",
    "not_found": "the source does not know this paper",
    "not_open_access": "the source knows the paper but has no free full-text copy",
    "abstract_only": "the source returned a record without the article body",
    "paywall": "the server answered with a login/landing page or a refusal instead of the file",
    "bot_wall": "the server answered with a bot check (CAPTCHA / Cloudflare); not bypassed by design",
    "rate_limited": "too many requests (HTTP 429); wait, or set the source's API key",
    "timeout": "the server did not answer in time",
    "network_error": "connection failed (DNS, TLS, proxy, offline)",
    "server_error": "the source had an internal error (HTTP 5xx)",
    "http_error": "unexpected HTTP status",
    "invalid_file": "something was downloaded but it is not a readable PDF/XML",
    "too_large": "the file is larger than max_download_mb",
    "blocked_url": "the URL was refused before any request (not http(s), or a private address)",
    "disabled": "this step is switched off in the config",
    "no_supplement": "the article has no supplementary files at this source",
    "not_supported": "not available through any API this project uses",
    "internal_error": "unexpected error inside this tool (a bug; please report it)",
}

RETRY_STATUS = {429, 500, 502, 503, 504}

# Minimum seconds between requests per host: (without key, with key).
# NCBI: 3 requests/s without a key, 10/s with one (https://www.ncbi.nlm.nih.gov/books/NBK25497/).
# arXiv: one request every 3 s (https://info.arxiv.org/help/api/tou.html).
# Semantic Scholar: 1 request/s with a key; the keyless pool is shared by everyone.
MIN_INTERVAL: dict[str, tuple[float, float]] = {
    "eutils.ncbi.nlm.nih.gov": (0.34, 0.11),
    "www.ncbi.nlm.nih.gov": (0.34, 0.34),
    "pmc.ncbi.nlm.nih.gov": (0.34, 0.34),
    "export.arxiv.org": (3.0, 3.0),
    "arxiv.org": (3.0, 3.0),
    "api.semanticscholar.org": (1.1, 1.1),
    "api.crossref.org": (0.1, 0.1),
    "api.openalex.org": (0.1, 0.1),
    "api.unpaywall.org": (0.1, 0.1),
    "www.ebi.ac.uk": (0.1, 0.1),
    "api.biorxiv.org": (0.2, 0.2),
}
_last_call: dict[str, float] = {}

SECRET_PARAMS = {"api_key", "apikey", "email", "mailto", "token", "access_token", "insttoken"}
_BOT_MARKERS = ("just a moment", "cf-challenge", "cf_chl", "captcha", "client challenge",
                "are you a robot", "verify you are human", "access denied - bot", "radware")


_extra_secrets: set[str] = set()  # the contact e-mail from config.yaml (registered by make_client)


def scrub(text: str) -> str:
    """Mask every configured key value and the contact e-mail in a text that is about to be shown or stored.
    Error bodies and exception messages come from third parties and may echo what was sent to them."""
    secrets = {os.environ.get(name, "").strip() for name in (*KEYS, "PAPEROPS_EMAIL")} | _extra_secrets
    for value in secrets:
        if len(value) >= 6:
            text = text.replace(value, "***")
    return text


class SourceError(Exception):
    """A source could not deliver. `reason` is one of REASONS."""

    def __init__(self, reason: str, detail: str = "", status: int | None = None, url: str | None = None):
        detail = scrub(detail)
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail
        self.status = status
        self.url = url


def redact_url(url: str) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return url
    query = [(k, "***" if k.lower() in SECRET_PARAMS else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit(parts._replace(query=urlencode(query, safe="*:/\"")))


def redact_params(params: dict | None) -> dict | None:
    if not params:
        return None
    return {k: ("***" if k.lower() in SECRET_PARAMS else v) for k, v in params.items()}


class ApiLog:
    """The API calls behind a result. Returned to the agent as `api_queries` and stored in SQLite."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def add(self, method: str, url: str, params: dict | None, status: int | None, note: str = "") -> None:
        entry: dict[str, Any] = {"method": method, "url": redact_url(url), "status": status}
        if params:
            entry["params"] = redact_params(params)
        if note:
            entry["note"] = note
        self.calls.append(entry)


# Key headers may only travel to the API they belong to, over https (httpx keeps custom headers on redirects).
_HEADER_DOMAINS = {"x-els-apikey": "elsevier.com", "x-els-insttoken": "elsevier.com",
                   "wiley-tdm-client-token": "wiley.com", "x-api-key": "semanticscholar.org"}


def _guard(request: httpx.Request) -> None:
    """Runs before every request, including each redirect hop."""
    check_url(str(request.url))
    host = request.url.host
    for name, domain in _HEADER_DOMAINS.items():
        if name in request.headers and not (
                request.url.scheme == "https" and (host == domain or host.endswith("." + domain))):
            del request.headers[name]


def make_client(timeout: float = 60, email: str = "") -> httpx.Client:
    """The contact e-mail is NOT put in the User-Agent (that would send it to every download host);
    it goes only to NCBI, Crossref and Unpaywall, as a request parameter."""
    if email:
        _extra_secrets.add(email)
    ua = f"Chaekkwae/{__version__} (literature retrieval; +https://github.com/Sejung98/Chaekkwae)"
    return httpx.Client(timeout=timeout, headers={"User-Agent": ua}, follow_redirects=True,
                        event_hooks={"request": [_guard]})


def throttle(url: str, has_key: bool = False, sleep: Callable[[float], None] = time.sleep,
             clock: Callable[[], float] = time.monotonic) -> None:
    host = urlsplit(url).hostname or ""
    rule = MIN_INTERVAL.get(host)
    if rule is None:
        return
    interval = rule[1] if has_key else rule[0]
    last = _last_call.get(host)
    if last is not None:
        wait = interval - (clock() - last)
        if wait > 0:
            sleep(wait)
    _last_call[host] = clock()


def _resolve(host: str) -> set[str]:
    try:
        return {str(ai[4][0]).split("%")[0] for ai in socket.getaddrinfo(host, None)}
    except OSError:
        return set()  # unresolvable: the request itself will fail with network_error


def check_url(url: str) -> None:
    """Refuse anything that is not a public http(s) URL. Download URLs come from third-party metadata and
    from redirects, so this runs on every hop and also on what the host name resolves to.
    ponytail: DNS rebinding between this check and the connection is not covered; pin the IP if that matters."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        raise SourceError("blocked_url", f"not an http(s) URL: {url[:120]}")
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        raise SourceError("blocked_url", f"local host name: {host}")
    try:
        addresses = {str(ipaddress.ip_address(host))}
    except ValueError:
        addresses = _resolve(host)
    for addr in addresses:
        if not ipaddress.ip_address(addr).is_global:
            raise SourceError("blocked_url", f"non-public address: {host}")


def _looks_like_bot_wall(resp: httpx.Response, head: str) -> bool:
    if resp.headers.get("cf-mitigated") == "challenge":
        return True
    low = head.lower()
    return any(m in low for m in _BOT_MARKERS)


def _status_error(resp: httpx.Response, auth: bool, url: str, body_head: str = "") -> SourceError:
    code = resp.status_code
    snippet = " ".join(body_head.split())[:200]
    if "<html" in body_head.lower() or "<!doctype" in body_head.lower():
        title = re.search(r"<title[^>]*>(.*?)</title>", body_head, re.I | re.S)
        snippet = "HTML page: " + (" ".join(title.group(1).split())[:120] if title else "(no title)")
    elif snippet.startswith("<"):  # XML error bodies (Elsevier, S3): keep the words, drop the tags
        snippet = " ".join(re.sub(r"<[^>]+>", " ", snippet).split())
    if code == 429:
        return SourceError("rate_limited", snippet, code, url)
    if code in (401, 403):
        if _looks_like_bot_wall(resp, body_head):
            return SourceError("bot_wall", snippet, code, url)
        return SourceError("key_rejected" if auth else "paywall", snippet, code, url)
    if code in (404, 410):
        return SourceError("not_found", snippet, code, url)
    if code >= 500:
        return SourceError("server_error", snippet, code, url)
    return SourceError("http_error", f"HTTP {code} {snippet}", code, url)


def _retry_after(resp: httpx.Response) -> float:
    try:
        return max(0.0, float(resp.headers.get("Retry-After", "")))
    except ValueError:
        return 0.0


def request(client: httpx.Client, log: ApiLog, method: str, url: str, *, params: dict | None = None,
            headers: dict | None = None, retries: int = 2, auth: bool = False,
            ok_status: tuple[int, ...] = (200,), sleep: Callable[[float], None] = time.sleep) -> httpx.Response:
    """API request with rate limiting and retries (429 / 5xx / network). Raises SourceError.

    auth=True means a key travels with the request, so 401/403 is reported as key_rejected."""
    check_url(url)
    last: SourceError | None = None
    for attempt in range(retries + 1):
        throttle(url, has_key=auth, sleep=sleep)
        try:
            resp = client.request(method, url, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            log.add(method, url, params, None, "timeout")
            last = SourceError("timeout", type(exc).__name__, None, redact_url(url))
        except httpx.TransportError as exc:
            log.add(method, url, params, None, type(exc).__name__)
            last = SourceError("network_error", f"{type(exc).__name__}: {exc}"[:200], None, redact_url(url))
        else:
            log.add(method, url, params, resp.status_code)
            if resp.status_code in ok_status:
                return resp
            last = _status_error(resp, auth, redact_url(str(resp.url)), resp.text[:2000])
            if resp.status_code not in RETRY_STATUS:
                raise last
            if attempt < retries:
                sleep(min(max(float(2 ** attempt), _retry_after(resp)), 30.0))
                continue
        if attempt < retries:
            sleep(float(2 ** attempt))
    assert last is not None
    raise last


def download(client: httpx.Client, log: ApiLog, url: str, dest: Path, *, headers: dict | None = None,
             params: dict | None = None, max_mb: float = 200, auth: bool = False,
             retries: int = 1, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Stream a file to `dest` (written to a .part file first). Returns size, hashes, content type.

    Content is not judged here beyond the HTTP status; see textio for PDF/XML validation."""
    check_url(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    limit = int(max_mb * 1024 * 1024)
    last: SourceError | None = None
    for attempt in range(retries + 1):
        throttle(url, has_key=auth, sleep=sleep)
        try:
            with client.stream("GET", url, headers=headers, params=params) as resp:
                final_url = redact_url(str(resp.url))
                if resp.status_code != 200:
                    head = next(resp.iter_bytes(2000), b"")[:2000].decode("utf-8", "replace")
                    log.add("GET", url, params, resp.status_code)
                    last = _status_error(resp, auth, final_url, head)
                    if resp.status_code not in RETRY_STATUS:
                        raise last
                else:
                    sha, md5, size = hashlib.sha256(), hashlib.md5(), 0
                    with open(part, "wb") as fh:
                        for chunk in resp.iter_bytes(65536):
                            size += len(chunk)
                            if size > limit:
                                raise SourceError("too_large", f"more than {max_mb} MB", 200, final_url)
                            sha.update(chunk)
                            md5.update(chunk)
                            fh.write(chunk)
                    os.replace(part, dest)
                    log.add("GET", url, params, 200, f"{size} bytes")
                    return {"path": str(dest), "bytes": size, "sha256": sha.hexdigest(), "md5": md5.hexdigest(),
                            "content_type": resp.headers.get("content-type", ""), "url": final_url}
        except httpx.TimeoutException as exc:
            log.add("GET", url, params, None, "timeout")
            last = SourceError("timeout", type(exc).__name__, None, redact_url(url))
        except httpx.TransportError as exc:
            log.add("GET", url, params, None, type(exc).__name__)
            last = SourceError("network_error", f"{type(exc).__name__}: {exc}"[:200], None, redact_url(url))
        finally:
            part.unlink(missing_ok=True)
        if attempt < retries:
            sleep(float(2 ** attempt))
    assert last is not None
    raise last


_HTML_START = re.compile(rb"^\s*(<!doctype html|<html|<head|<script)", re.I)


def classify_not_a_file(path: Path) -> SourceError:
    """Explain a download that turned out to be a web page instead of the expected file."""
    with open(path, "rb") as fh:
        head = fh.read(8192)
    text = head.decode("utf-8", "replace").lower()
    if any(m in text for m in _BOT_MARKERS):
        return SourceError("bot_wall", "got a bot-check page instead of the file")
    if _HTML_START.match(head) or b"<html" in head[:2000].lower():
        return SourceError("paywall", "got a web page (login or landing page) instead of the file")
    return SourceError("invalid_file", "unrecognised content: " + repr(head[:40]))
