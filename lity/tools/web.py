"""Web tools: DuckDuckGo HTML search (no API key — plug-and-play) and page fetch."""

import html as html_mod
import re

import httpx

from . import params, tool

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Lity/0.1"}


async def ddg_search(query: str, limit: int = 5) -> list[dict]:
    async with httpx.AsyncClient(timeout=20, headers=UA, follow_redirects=True) as c:
        r = await c.post("https://html.duckduckgo.com/html/", data={"q": query})
        r.raise_for_status()
    page = r.text
    results = []
    for m in re.finditer(
            r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>',
            page, re.S):
        url, title, snippet = m.groups()
        # DDG wraps URLs in a redirect (uddg=)
        real = re.search(r'uddg=([^&]+)', url)
        if real:
            from urllib.parse import unquote
            url = unquote(real.group(1))
        results.append({
            "url": url,
            "title": html_mod.unescape(re.sub(r"<[^>]+>", "", title)).strip(),
            "snippet": html_mod.unescape(re.sub(r"<[^>]+>", "", snippet)).strip(),
        })
        if len(results) >= limit:
            break
    return results


def strip_html(page: str, max_chars: int = 8000) -> str:
    page = re.sub(r"(?is)<(script|style|noscript|svg|header|footer|nav)[^>]*>.*?</\1>", " ", page)
    page = re.sub(r"(?s)<[^>]+>", " ", page)
    page = html_mod.unescape(page)
    page = re.sub(r"[ \t]+", " ", page)
    page = re.sub(r"\n\s*\n+", "\n\n", page)
    text = page.strip()
    return text if len(text) <= max_chars else text[:max_chars] + "\n…[truncated]"


@tool("web_search", "Search the web (DuckDuckGo). Returns titles, URLs and snippets.",
      params({"query": {"type": "string"},
              "limit": {"type": "integer", "description": "max results, default 5"}},
             required=["query"]), level=1, direct=True)
async def web_search(ctx, args):
    results = await ddg_search(args["query"], int(args.get("limit", 5)))
    if not results:
        return "No results."
    return "\n\n".join(f"{r['title']}\n{r['url']}\n{r['snippet']}" for r in results)


@tool("fetch_url", "Fetch a URL and return its readable text content.",
      params({"url": {"type": "string"}}), level=1)
async def fetch_url(ctx, args):
    async with httpx.AsyncClient(timeout=30, headers=UA, follow_redirects=True) as c:
        r = await c.get(args["url"])
        r.raise_for_status()
    ctype = r.headers.get("content-type", "")
    if "html" not in ctype and "text" not in ctype and "json" not in ctype:
        return f"Unsupported content type: {ctype}"
    return strip_html(r.text)
