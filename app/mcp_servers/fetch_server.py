# app/mcp_servers/fetch_server.py
from __future__ import annotations

import re
from html.parser import HTMLParser

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fetch_server")

DEFAULT_MAX_LENGTH = 5000
DEFAULT_TIMEOUT_SECONDS = 15.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
BLOCKED_STATUSES = {403, 429}


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip = False

    def handle_data(self, data: str) -> None:
        if not self._skip and data:
            self._chunks.append(data)

    def get_text(self) -> str:
        return " ".join(self._chunks)


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _to_text(body: str, content_type: str | None) -> str:
    if content_type and "text/html" in content_type:
        parser = _HTMLTextExtractor()
        parser.feed(body)
        return _collapse_ws(parser.get_text())
    return body


def _truncate(text: str, max_length: int) -> str:
    if max_length <= 0:
        return text
    return text[:max_length]


def _jina_reader_url(url: str) -> str:
    return f"https://r.jina.ai/http://{url}"


def _is_http_url(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")


def _is_jina_url(url: str) -> bool:
    return "r.jina.ai/http://" in url


async def _request_text(url: str) -> tuple[int, str, str | None]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=DEFAULT_TIMEOUT_SECONDS
    ) as client:
        resp = await client.get(url, headers=headers)
        return resp.status_code, resp.text, resp.headers.get("content-type")


async def _fetch_with_jina(url: str, max_length: int) -> str:
    jina_url = _jina_reader_url(url)
    status, body, content_type = await _request_text(jina_url)
    if status >= 400:
        return f"Failed to fetch {url} - status code {status}"
    text = _to_text(body, content_type)
    return _truncate(text, max_length)


@mcp.tool()
async def fetch(url: str, max_length: int = DEFAULT_MAX_LENGTH) -> str:
    """Fetch a URL and return plain text (best effort).

    Args:
        url: HTTP/HTTPS URL to fetch
        max_length: Max number of characters to return
    """
    if not _is_http_url(url):
        return f"Invalid URL: {url}"

    safe_max = int(max_length or DEFAULT_MAX_LENGTH)
    try:
        status, body, content_type = await _request_text(url)
        if status >= 400:
            if status in BLOCKED_STATUSES and not _is_jina_url(url):
                return await _fetch_with_jina(url, safe_max)
            return f"Failed to fetch {url} - status code {status}"
        text = _to_text(body, content_type)
        return _truncate(text, safe_max)
    except Exception as e:
        if not _is_jina_url(url):
            try:
                return await _fetch_with_jina(url, safe_max)
            except Exception:
                pass
        return f"Failed to fetch {url} - {type(e).__name__}: {e}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
