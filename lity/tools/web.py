"""DuckDuckGo HTML search (no API key — plug-and-play) for the kernel's
quick_search tool. Anything deeper than a snippet lookup goes to Hermes."""

import html as html_mod
import re

import httpx

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


