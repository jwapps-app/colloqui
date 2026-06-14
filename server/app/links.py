"""Server-side link previews: fetch a URL's title/description (text only, no
images) for cards. Built to be safe against SSRF — it refuses to fetch private,
loopback, link-local, or otherwise non-public addresses, validates every
redirect hop, and caps response size and time."""
import asyncio
import html
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse

import httpx

URL_RE = re.compile(r'https?://[^\s<>"\')]+', re.I)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_META_RE = re.compile(r"<meta\s+([^>]+?)/?>", re.I | re.S)
_ATTR_RE = re.compile(
    r'([\w:-]+)\s*=\s*"([^"]*)"' r"|([\w:-]+)\s*=\s*'([^']*)'", re.S
)

_UA = {"User-Agent": "ColloquiBot/1.0 (+link preview)"}
_MAX_BYTES = 512_000
_TIMEOUT = 5.0
_MAX_REDIRECTS = 4


def extract_urls(text: str, limit: int = 3) -> list[str]:
    """Distinct http(s) URLs in order of appearance, trailing punctuation trimmed."""
    seen: list[str] = []
    for raw in URL_RE.findall(text or ""):
        url = raw.rstrip(".,;:!?")
        if url not in seen:
            seen.append(url)
        if len(seen) >= limit:
            break
    return seen


async def _host_is_public(host: str) -> bool:
    if not host:
        return False
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


def _meta_map(head: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for attrs in _META_RE.findall(head):
        pairs: dict[str, str] = {}
        for m in _ATTR_RE.finditer(attrs):
            key = (m.group(1) or m.group(3) or "").lower()
            val = m.group(2) if m.group(2) is not None else m.group(4)
            if key:
                pairs[key] = val or ""
        key = pairs.get("property") or pairs.get("name")
        if key and "content" in pairs:
            out.setdefault(key.lower(), pairs["content"])
    return out


def _clean(s: str | None, limit: int) -> str | None:
    if not s:
        return None
    s = html.unescape(re.sub(r"\s+", " ", s)).strip()
    return s[:limit] or None


async def fetch_metadata(url: str) -> dict | None:
    """Return {title, description, site_name} for a public http(s) URL, or None."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    try:
        async with httpx.AsyncClient(
            follow_redirects=False, timeout=_TIMEOUT, headers=_UA
        ) as client:
            for _ in range(_MAX_REDIRECTS):
                if parsed.scheme not in ("http", "https"):
                    return None
                if not await _host_is_public(parsed.hostname or ""):
                    return None
                async with client.stream("GET", url) as resp:
                    if resp.is_redirect:
                        loc = resp.headers.get("location")
                        if not loc:
                            return None
                        url = urljoin(url, loc)
                        parsed = urlparse(url)
                        continue
                    if "html" not in resp.headers.get("content-type", "").lower():
                        return None
                    body = bytearray()
                    async for chunk in resp.aiter_bytes():
                        body.extend(chunk)
                        if len(body) >= _MAX_BYTES:
                            break
                    text = bytes(body).decode(resp.encoding or "utf-8", "replace")
                    break
            else:
                return None
    except Exception:
        # Any network/TLS/parse failure → just no preview.
        return None

    head = text[:_MAX_BYTES]
    meta = _meta_map(head)
    title_tag = _TITLE_RE.search(head)
    title = _clean(meta.get("og:title") or (title_tag.group(1) if title_tag else None), 200)
    description = _clean(meta.get("og:description") or meta.get("description"), 300)
    site_name = _clean(meta.get("og:site_name"), 100) or (parsed.hostname or None)
    if not title and not description:
        return None
    return {"title": title, "description": description, "site_name": site_name}
