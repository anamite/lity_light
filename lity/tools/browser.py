"""Playwright browser tools for the browser sub-agent. Chromium is launched
lazily on first use and shared for the process lifetime."""

import time

from . import params, tool

_state = {"pw": None, "browser": None, "page": None}


async def _page(ctx):
    if _state["page"]:
        return _state["page"]
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError("Playwright is not installed. Run: pip install playwright && playwright install chromium")
    _state["pw"] = await async_playwright().start()
    _state["browser"] = await _state["pw"].chromium.launch(headless=True)
    _state["page"] = await _state["browser"].new_page(viewport={"width": 1280, "height": 900})
    return _state["page"]


async def shutdown():
    if _state["browser"]:
        await _state["browser"].close()
    if _state["pw"]:
        await _state["pw"].stop()
    _state.update(pw=None, browser=None, page=None)


SNAPSHOT_JS = """() => {
  const pick = (els, fmt, max) => Array.from(els).slice(0, max).map(fmt);
  const links = pick(document.querySelectorAll('a[href]'),
    a => `[link] ${a.innerText.trim().slice(0,60)} -> ${a.getAttribute('href')}`, 40);
  const inputs = pick(document.querySelectorAll('input, textarea, select'),
    i => `[input] selector: ${i.tagName.toLowerCase()}${i.id ? '#'+i.id : i.name ? `[name="${i.name}"]` : ''} type=${i.type||''} placeholder=${i.placeholder||''}`, 25);
  const buttons = pick(document.querySelectorAll('button, [role="button"], input[type=submit]'),
    b => `[button] selector: ${b.tagName.toLowerCase()}${b.id ? '#'+b.id : ''} text=${(b.innerText||b.value||'').trim().slice(0,40)}`, 25);
  return {title: document.title, url: location.href,
          text: document.body ? document.body.innerText.slice(0, 4000) : '',
          links, inputs, buttons};
}"""


@tool("browser_goto", "Navigate the browser to a URL.",
      params({"url": {"type": "string"}}), level=1)
async def browser_goto(ctx, args):
    page = await _page(ctx)
    await page.goto(args["url"], wait_until="domcontentloaded", timeout=45000)
    return f"Now at {page.url} — take a browser_snapshot to see the page."


@tool("browser_snapshot", "Read the current page: visible text plus link/input/button selectors.",
      params({}, required=[]), level=1)
async def browser_snapshot(ctx, args):
    page = await _page(ctx)
    s = await page.evaluate(SNAPSHOT_JS)
    parts = [f"# {s['title']}\nURL: {s['url']}\n\n{s['text']}"]
    for key in ("buttons", "inputs", "links"):
        if s[key]:
            parts.append("\n".join(s[key]))
    return "\n\n".join(parts)


@tool("browser_click", "Click an element by CSS selector (state-changing — permission gated).",
      params({"selector": {"type": "string"}}), level=3)
async def browser_click(ctx, args):
    page = await _page(ctx)
    await page.click(args["selector"], timeout=10000)
    await page.wait_for_load_state("domcontentloaded", timeout=15000)
    return f"Clicked {args['selector']}. Now at {page.url} — snapshot to verify."


@tool("browser_type", "Type text into an element by CSS selector (clears it first).",
      params({"selector": {"type": "string"}, "text": {"type": "string"}}), level=3)
async def browser_type(ctx, args):
    page = await _page(ctx)
    await page.fill(args["selector"], args["text"], timeout=10000)
    return f"Typed into {args['selector']}."


@tool("browser_screenshot", "Save a screenshot of the current page to the workspace; returns the path.",
      params({}, required=[]), level=1)
async def browser_screenshot(ctx, args):
    page = await _page(ctx)
    name = f"screenshots/shot_{int(time.time())}.png"
    path = ctx.app.cfg.workspace / name
    path.parent.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(path))
    return f"Screenshot saved: {name}"
