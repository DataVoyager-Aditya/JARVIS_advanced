"""
Phase 10.A — content acquisition for the deep researcher.

Everything here is free and degrades softly (a dead source is skipped, never fatal):
  * tavily_search   — discovery via Tavily (rotated keys, the same pool web_search uses).
  * fetch_url       — read a page with httpx (real browser UA, redirects) -> trafilatura extracts
                      the MAIN text (no nav/ads), plus the outbound links (for multi-hop).
  * browser_fetch   — fallback for JS-heavy pages httpx gets thin/blank from: a headless Playwright
                      Chromium renders it, scrolls to trigger lazy content, then we extract. Optional
                      — if Playwright/Chromium aren't present it's skipped cleanly (never a crash).
  * source_grade    — domain-reputation score (gov/edu/Wikipedia/known publishers > random blogs),
                      used to weight claims and rank sources in the briefing.

NOTHING here raises on a network/parse failure to the caller — it returns None/empty so the engine
just moves on to the next source. All fetches are bounded by a timeout and a max page size.
"""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlparse

import httpx

from config import (
    load_key_pool, RESEARCH_FETCH_TIMEOUT_S, RESEARCH_MAX_PAGE_CHARS, RESEARCH_MIN_PAGE_CHARS,
)

logger = logging.getLogger("jarvis.research.fetch")

_TAVILY_KEYS = load_key_pool("TAVILY_API_KEY")


def has_search() -> bool:
    """True if web discovery is possible (a Tavily key is configured). Without it a sweep can't find
    sources, so the tool tells him upfront rather than starting a doomed background job."""
    return bool(_TAVILY_KEYS)

_MAX_FETCH_BYTES = 6_000_000   # never pull more than ~6 MB of a page into memory (a giant file/blob)

# A real desktop UA + headers — many sites 403 the default httpx/python UA outright.
_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Domains we never bother fetching (paywalled hard, login walls, JS-app shells with no text, or
# noise) — Tavily sometimes returns them; reading them wastes a slot. Matched as a suffix.
_SKIP_DOMAINS = (
    "facebook.com", "instagram.com", "x.com", "twitter.com", "tiktok.com", "linkedin.com",
    "pinterest.com", "youtube.com", "youtu.be",
)

# --- Source-trust tiers (domain reputation). Suffix match on the registrable-ish host. --------- #
_TRUST_HIGH = {
    "wikipedia.org", "nature.com", "science.org", "sciencedirect.com", "ncbi.nlm.nih.gov",
    "nih.gov", "who.int", "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk", "npr.org",
    "nytimes.com", "wsj.com", "economist.com", "ft.com", "bloomberg.com", "theguardian.com",
    "arxiv.org", "ieee.org", "acm.org", "pnas.org", "mit.edu", "stanford.edu", "harvard.edu",
    "nasa.gov", "noaa.gov", "europa.eu", "oecd.org", "imf.org", "worldbank.org",
}
_TRUST_MED = {
    "arstechnica.com", "theverge.com", "wired.com", "techcrunch.com", "cnbc.com", "forbes.com",
    "businessinsider.com", "axios.com", "politico.com", "vox.com", "theatlantic.com",
    "medium.com", "github.com", "githubusercontent.com", "stackoverflow.com", "investopedia.com",
    "britannica.com", "pewresearch.org", "statista.com", "ourworldindata.org",
}


def _host(url: str) -> str:
    try:
        h = (urlparse(url).hostname or "").lower()
        return h[4:] if h.startswith("www.") else h   # strip the LITERAL "www." prefix only
    except Exception:  # noqa: BLE001
        return ""


def _suffix_match(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def source_grade(url: str) -> tuple[float, str]:
    """Return (trust_score 0..1, label). Reputable reference/press/gov/edu rank high; .gov/.edu TLDs
    are trusted generically; known quality press is medium-high; everything else is a neutral middle
    (we don't punish an unknown domain to zero — it just isn't given extra weight)."""
    host = _host(url)
    if not host:
        return 0.4, "unverified"
    if _suffix_match(host, _TRUST_HIGH) or host.endswith(".gov") or host.endswith(".edu") \
            or host.endswith(".gov.in") or host.endswith(".ac.uk") or host.endswith(".edu.au"):
        return 0.95, "authoritative"
    if _suffix_match(host, _TRUST_MED):
        return 0.7, "reputable"
    if host.endswith(".org"):
        return 0.6, "organisation"
    return 0.45, "general"


def skippable(url: str) -> bool:
    host = _host(url)
    return not host or _suffix_match(host, _SKIP_DOMAINS) or not url.lower().startswith("http")


# ------------------------------------------------------------------ #
# Discovery — Tavily search (rotated keys)
# ------------------------------------------------------------------ #
async def tavily_search(query: str, max_results: int = 5) -> list[dict]:
    """Return [{url,title,content,score}] for a query. Tries each key; [] if all fail / none set."""
    if not _TAVILY_KEYS:
        return []
    last = None
    for key in _TAVILY_KEYS:
        try:
            async with httpx.AsyncClient(timeout=RESEARCH_FETCH_TIMEOUT_S) as c:
                r = await c.post("https://api.tavily.com/search", json={
                    # "basic" (1 credit) not "advanced" (2): we read the full pages ourselves with
                    # trafilatura + multi-hop, so Tavily only needs to DISCOVER urls — paying double
                    # would halve the free monthly quota and let daily monitors silently exhaust it.
                    "api_key": key, "query": query[:380], "max_results": max(1, max_results),
                    "search_depth": "basic", "include_answer": False,
                })
            r.raise_for_status()
            out = []
            for res in r.json().get("results", []):
                u = (res.get("url") or "").strip()
                if u and not skippable(u):
                    out.append({"url": u, "title": (res.get("title") or "").strip(),
                                "content": (res.get("content") or "").strip(),
                                "score": float(res.get("score") or 0.0)})
            return out
        except Exception as e:  # noqa: BLE001
            last = e
            logger.warning("tavily search key failed (%s) — rotating", type(e).__name__)
    logger.warning("tavily search exhausted for %r: %s", query[:60], last)
    return []


# ------------------------------------------------------------------ #
# Reading — extract main text + outbound links
# ------------------------------------------------------------------ #
def _extract_text(html: str, url: str) -> str:
    """Main readable text via trafilatura (nav/ads stripped). Empty string if nothing usable."""
    if not html:
        return ""
    try:
        import trafilatura
        txt = trafilatura.extract(
            html, url=url, include_comments=False, include_tables=True,
            favor_recall=True, no_fallback=False,
        )
        return (txt or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _extract_links(html: str, base_url: str, limit: int = 40) -> list[str]:
    """Outbound absolute http(s) links on the page, deduped, for multi-hop following."""
    if not html:
        return []
    try:
        from lxml import html as lxml_html
        doc = lxml_html.fromstring(html)
        doc.make_links_absolute(base_url, resolve_base_href=True)
        seen, out = set(), []
        for a in doc.xpath("//a[@href]"):
            href = (a.get("href") or "").split("#")[0].strip()
            if href.lower().startswith("http") and href not in seen and not skippable(href):
                seen.add(href)
                out.append(href)
                if len(out) >= limit:
                    break
        return out
    except Exception:  # noqa: BLE001
        return []


async def fetch_url(url: str, timeout: float = RESEARCH_FETCH_TIMEOUT_S) -> dict | None:
    """Read a page with httpx. Returns {url(final), text, links} or None on failure / non-HTML /
    empty extraction. `text` is capped; `links` feeds the multi-hop step."""
    if skippable(url):
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                     headers=_BROWSER_HEADERS) as c:
            # Stream so we can reject a non-HTML/huge body by its CONTENT-TYPE/size BEFORE pulling
            # the whole thing into memory (a multi-hundred-MB file would otherwise OOM the worker).
            async with c.stream("GET", url) as r:
                if r.status_code != 200:
                    return None
                ctype = r.headers.get("content-type", "").lower()
                if ctype and "html" not in ctype and "text" not in ctype and "xml" not in ctype:
                    return None
                final_url = str(r.url)
                buf, total = [], 0
                async for chunk in r.aiter_bytes():
                    buf.append(chunk)
                    total += len(chunk)
                    if total >= _MAX_FETCH_BYTES:
                        break
                enc = r.charset_encoding or "utf-8"
        html = b"".join(buf).decode(enc, "ignore")
    except Exception as e:  # noqa: BLE001
        logger.debug("fetch_url failed %s (%s)", url, type(e).__name__)
        return None
    # Parsing (trafilatura + lxml) is CPU-bound; run it off the worker event loop so concurrent
    # sweeps aren't serialized behind one page's parse.
    text = await asyncio.to_thread(_extract_text, html, final_url)
    links = await asyncio.to_thread(_extract_links, html, final_url)
    if not text:
        return {"url": final_url, "text": "", "links": links, "thin": True}
    return {"url": final_url, "text": text[:RESEARCH_MAX_PAGE_CHARS], "links": links,
            "thin": len(text) < RESEARCH_MIN_PAGE_CHARS}


# ------------------------------------------------------------------ #
# Browser-assisted fetch (Playwright) — for JS-heavy pages httpx can't read
# ------------------------------------------------------------------ #
_browser_ok: bool | None = None   # cached probe: is Playwright+Chromium actually usable here?


def browser_available() -> bool:
    global _browser_ok
    if _browser_ok is not None:
        return _browser_ok
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        # Chromium presence is verified lazily on first real launch; importing is the cheap gate.
        _browser_ok = True
    except Exception:  # noqa: BLE001
        _browser_ok = False
        logger.info("Playwright not available — browser-assisted research disabled (httpx only)")
    return _browser_ok


async def browser_fetch(url: str, timeout: float = RESEARCH_FETCH_TIMEOUT_S) -> dict | None:
    """Render a JS-heavy page in headless Chromium, scroll to trigger lazy content, dismiss the
    obvious cookie banner, then extract. Returns {url,text,links} or None. Best-effort and fully
    isolated: any failure (Chromium missing, nav timeout) just returns None."""
    if not browser_available() or skippable(url):
        return None
    try:
        from playwright.async_api import async_playwright
    except Exception:  # noqa: BLE001
        return None
    ms = int(max(5.0, timeout) * 1000)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page(user_agent=_BROWSER_HEADERS["User-Agent"])
                await page.goto(url, wait_until="domcontentloaded", timeout=ms)
                # Best-effort cookie-banner dismissal (don't fail if none).
                for label in ("Accept all", "Accept", "I agree", "Got it", "Allow all"):
                    try:
                        btn = page.get_by_role("button", name=re.compile(label, re.I))
                        if await btn.count():
                            await btn.first.click(timeout=1200)
                            break
                    except Exception:  # noqa: BLE001
                        pass
                # Nudge lazy content + "load more" by scrolling a few viewports.
                for _ in range(4):
                    try:
                        await page.mouse.wheel(0, 4000)
                        await page.wait_for_timeout(350)
                    except Exception:  # noqa: BLE001
                        break
                html = await page.content()
                final_url = page.url
            finally:
                await browser.close()
    except Exception as e:  # noqa: BLE001
        # If Chromium itself is missing (package installed but `playwright install chromium` never
        # run), disable the browser path for the rest of the process so we don't keep burning the
        # per-sweep browser budget on launches that can never succeed.
        msg = str(e).lower()
        if "executable" in msg or "doesn't exist" in msg or "install" in msg:
            global _browser_ok
            _browser_ok = False
            logger.info("Chromium binary unavailable — disabling browser-assisted research")
        else:
            logger.debug("browser_fetch failed %s (%s)", url, type(e).__name__)
        return None
    text = await asyncio.to_thread(_extract_text, html, final_url)
    links = await asyncio.to_thread(_extract_links, html, final_url)
    if not text:
        return None
    return {"url": final_url, "text": text[:RESEARCH_MAX_PAGE_CHARS], "links": links, "thin": False}


async def read_page(url: str, allow_browser: bool = True) -> dict | None:
    """Read a page the smart way: httpx first; if the result is thin/blank AND a browser is allowed
    and available, retry with a headless render. Returns the richer of the two, or None."""
    res = await fetch_url(url)
    if res and not res.get("thin") and res.get("text"):
        return res
    if allow_browser:
        rendered = await browser_fetch(url)
        if rendered and rendered.get("text"):
            return rendered
    return res if (res and res.get("text")) else None
