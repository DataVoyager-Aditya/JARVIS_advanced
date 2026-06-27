"""
Phase 10.A — the Deep Researcher.

One sweep, end to end (all free, all bounded):
  1. PLAN      — an LLM decomposes the topic into a handful of focused sub-questions.
  2. GATHER    — Tavily finds sources per question; httpx+trafilatura read them; thin/JS pages get a
                 headless-browser render. Multi-hop follows cited links one+ level deep. Bounded by a
                 source cap, a per-host cap, a browser cap, and an overall wall-clock budget.
  3. INDEX     — every page is chunked and embedded into a TRANSIENT, in-memory FAISS index (local
                 MiniLM — no key, no cost); the chunks most relevant to the topic/questions are picked
                 (lexical fallback if the embedder isn't up), capped so synthesis stays small + fast.
  4. SYNTHESIZE— the LLM reads ONLY those chunks (tagged with their source + trust grade) and writes a
                 structured briefing: a spoken digest, exec summary, key findings (cited), contradictions,
                 and a confidence rating. Sources are appended from the REAL fetched set (never invented).

`Researcher.run(topic)` returns a briefing dict. It never raises for an ordinary failure (no key, no
results, a flaky source, an LLM hiccup) — it degrades to an honest, sourced, lower-confidence result.
`on_progress(line)` is called with short, speakable milestones the listener narrates live.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections import Counter
from urllib.parse import urlparse

from config import (
    RESEARCH_MAX_QUESTIONS, RESEARCH_RESULTS_PER_Q, RESEARCH_MAX_SOURCES, RESEARCH_DEPTH,
    RESEARCH_HOP_LINKS, RESEARCH_FETCH_CONCURRENCY, RESEARCH_TIME_BUDGET_S, RESEARCH_CHUNK_CHARS,
    RESEARCH_SYNTH_CHUNKS, RESEARCH_BROWSER, RESEARCH_BROWSER_MAX,
)
from app.services.research import fetch as F

logger = logging.getLogger("jarvis.research.engine")

_WORD_RE = re.compile(r"[a-z][a-z0-9'\-]{2,}")
_STOP = {"the", "and", "for", "are", "with", "what", "how", "why", "when", "where", "which",
         "this", "that", "from", "into", "about", "over", "your", "you", "his", "her", "its",
         "their", "them", "they", "will", "would", "could", "should", "have", "has", "had",
         "been", "being", "than", "then", "also", "more", "most", "some", "any", "all", "can"}
_MAX_PER_HOST = 3                      # don't let one domain dominate a sweep


def _host(url: str) -> str:
    try:
        h = (urlparse(url).hostname or "").lower()
        return h[4:] if h.startswith("www.") else h   # strip the LITERAL "www." prefix only
    except Exception:  # noqa: BLE001
        return ""


def _keywords(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall((text or "").lower()) if w not in _STOP}


class Researcher:
    def __init__(self, rotator, on_progress=None) -> None:
        self.rotator = rotator
        self._on_progress = on_progress
        self._t0 = 0.0
        self._browser_budget = RESEARCH_BROWSER_MAX if RESEARCH_BROWSER else 0

    # -- progress narration (best-effort; never breaks the sweep) -- #
    def _say(self, line: str) -> None:
        if self._on_progress:
            try:
                self._on_progress(line)
            except Exception:  # noqa: BLE001
                logger.debug("on_progress raised", exc_info=True)

    def _over_budget(self) -> bool:
        return (time.monotonic() - self._t0) > RESEARCH_TIME_BUDGET_S

    # ------------------------------------------------------------------ #
    async def run(self, topic: str) -> dict:
        self._t0 = time.monotonic()
        topic = (topic or "").strip()
        if not topic:
            return self._empty(topic, "There was no topic to research.")

        questions = await self._plan(topic)
        # (No "starting" line here — the deep_research tool already spoke the kickoff ack; a second
        # line right after it would collide. The first live update is the mid-sweep milestone below.)
        sources = await self._gather(topic, questions)
        if not sources:
            return self._empty(topic, "No readable sources came back for this topic.")

        hosts = len({_host(s["url"]) for s in sources})
        self._say(f"{len(sources)} sources read across {hosts} sites — cross-referencing now.")

        chunks = self._chunk_sources(sources)
        top = await self._select_chunks(topic, questions, chunks)
        briefing = await self._synthesize(topic, questions, top, sources)
        secs = int(time.monotonic() - self._t0)
        logger.info("research '%s' done in %ds — %d sources, %d chunks", topic[:60], secs,
                    len(sources), len(top))
        return briefing

    # ------------------------------------------------------------------ #
    # 1. PLAN
    # ------------------------------------------------------------------ #
    async def _plan(self, topic: str) -> list[str]:
        prompt = (
            "You are a meticulous research analyst. Break the user's topic into "
            f"{RESEARCH_MAX_QUESTIONS} focused, NON-overlapping sub-questions that together would "
            "produce a thorough, well-rounded briefing (cover background, current state, key "
            "players/data, controversy or risks, and outlook where relevant). Return ONLY a JSON "
            'array of question strings, nothing else.\n\nTOPIC: ' + topic
        )
        try:
            raw = await self.rotator.chat(
                [{"role": "system", "content": "You output only valid JSON when asked."},
                 {"role": "user", "content": prompt}],
                task="chat", temperature=0.3)
        except Exception as e:  # noqa: BLE001
            logger.warning("plan LLM failed (%s) — using generic angles", type(e).__name__)
            raw = ""
        qs = self._parse_questions(raw)
        if not qs:
            qs = [topic,
                  f"{topic} latest developments and current status",
                  f"{topic} key facts, data and main players",
                  f"{topic} risks, criticism or controversy"]
        # Always include the bare topic so the most direct search is covered.
        if topic.lower() not in {q.lower() for q in qs}:
            qs.insert(0, topic)
        return qs[:RESEARCH_MAX_QUESTIONS]

    @staticmethod
    def _parse_questions(raw: str) -> list[str]:
        raw = (raw or "").strip()
        out: list[str] = []
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if m:
            try:
                import json
                arr = json.loads(m.group(0))
                if isinstance(arr, list):
                    out = [str(x).strip() for x in arr if str(x).strip()]
            except Exception:  # noqa: BLE001
                out = []
        if not out:   # fall back to line parsing (numbered/bulleted)
            for ln in raw.splitlines():
                ln = re.sub(r"^\s*(?:\d+[.)]|[-*•])\s*", "", ln).strip().strip('"')
                if len(ln) > 8 and ln.endswith("?") or (len(ln) > 12 and "?" in ln):
                    out.append(ln)
        # dedup, keep order, cap length
        seen, uniq = set(), []
        for q in out:
            k = q.lower()
            if k not in seen:
                seen.add(k)
                uniq.append(q[:200])
        return uniq[:RESEARCH_MAX_QUESTIONS]

    # ------------------------------------------------------------------ #
    # 2. GATHER (search + read + multi-hop)
    # ------------------------------------------------------------------ #
    async def _gather(self, topic: str, questions: list[str]) -> list[dict]:
        # Discover seed URLs by searching each question (concurrently).
        searches = await asyncio.gather(
            *[F.tavily_search(q, RESEARCH_RESULTS_PER_Q) for q in questions],
            return_exceptions=True)
        seeds: list[dict] = []          # {url, title, score, snippet}
        seen_urls: set[str] = set()
        for res in searches:
            if isinstance(res, Exception) or not res:
                continue
            for r in res:
                u = r["url"]
                if u not in seen_urls:
                    seen_urls.add(u)
                    seeds.append(r)
        if not seeds:
            return []
        # Rank seeds: search score + source-trust grade. Read the best first.
        seeds.sort(key=lambda r: (r.get("score", 0.0) + F.source_grade(r["url"])[0]), reverse=True)

        sources: list[dict] = []
        host_count: Counter = Counter()
        visited: set[str] = set()
        topic_kw = _keywords(topic + " " + " ".join(questions))

        # Wave 1: seeds. Wave 2..DEPTH: harvested cited links.
        frontier = [s["url"] for s in seeds]
        title_by_url = {s["url"]: s.get("title", "") for s in seeds}
        sem = asyncio.Semaphore(RESEARCH_FETCH_CONCURRENCY)

        for depth in range(1, RESEARCH_DEPTH + 1):
            if not frontier or len(sources) >= RESEARCH_MAX_SOURCES or self._over_budget():
                break
            # Filter the frontier by caps before reading.
            batch: list[str] = []
            for url in frontier:
                if url in visited:
                    continue
                h = _host(url)
                if not h or host_count[h] >= _MAX_PER_HOST:
                    continue
                visited.add(url)
                host_count[h] += 1
                batch.append(url)
                if len(sources) + len(batch) >= RESEARCH_MAX_SOURCES:
                    break
            if not batch:
                break
            # Read this wave concurrently. Browser fallback only on the seed wave (depth 1) — it's
            # slow, so cited-link hops stay httpx-only — and only while the browser budget remains.
            # Bound the wave by the REMAINING time budget so an in-flight slow page can't push a
            # sweep far past RESEARCH_TIME_BUDGET_S; completed reads are harvested, the rest cancelled.
            allow_browser = depth == 1
            remaining = RESEARCH_TIME_BUDGET_S - (time.monotonic() - self._t0)
            tasks = [asyncio.ensure_future(self._read_one(u, sem, allow_browser)) for u in batch]
            pages: list = [None] * len(batch)
            if remaining > 0:
                done, pending = await asyncio.wait(tasks, timeout=remaining)
                for i, t in enumerate(tasks):
                    if t in done and not t.cancelled():
                        try:
                            pages[i] = t.result()
                        except Exception:  # noqa: BLE001
                            pages[i] = None
                for t in pending:
                    t.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)   # let cancellations settle
            else:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

            harvested: list[str] = []
            for url, page in zip(batch, pages):
                if isinstance(page, Exception) or not page or not page.get("text"):
                    continue
                final = page["url"]
                grade, label = F.source_grade(final)
                sources.append({
                    "url": final, "title": title_by_url.get(url, "") or _host(final),
                    "text": page["text"], "grade": grade, "label": label,
                })
                harvested.extend(page.get("links", []))
                if len(sources) >= RESEARCH_MAX_SOURCES:
                    break

            # Build next wave from harvested links, ranked by domain trust + topic-keyword hit in URL.
            if depth < RESEARCH_DEPTH and harvested and len(sources) < RESEARCH_MAX_SOURCES:
                ranked = self._rank_links(harvested, visited, host_count, topic_kw)
                frontier = ranked[:RESEARCH_HOP_LINKS]
            else:
                frontier = []
        return sources[:RESEARCH_MAX_SOURCES]

    async def _read_one(self, url: str, sem: asyncio.Semaphore, allow_browser: bool):
        """Read one page: httpx first; if it comes back thin/blank and a browser render is allowed
        and budget remains, retry with a headless render. The browser budget slot is reserved
        BEFORE the await (no suspension between check and decrement) so concurrent reads can't both
        claim the last slot."""
        async with sem:
            if self._over_budget():
                return None
            page = await F.fetch_url(url)
            thin = (page is None) or page.get("thin") or not page.get("text")
            if thin and allow_browser and self._browser_budget > 0 and not self._over_budget():
                self._browser_budget -= 1            # reserve atomically
                rendered = await F.browser_fetch(url)
                if rendered and rendered.get("text"):
                    return rendered
            return page if (page and page.get("text")) else None

    @staticmethod
    def _rank_links(links: list[str], visited: set[str], host_count: Counter,
                    topic_kw: set[str]) -> list[str]:
        scored: list[tuple[float, str]] = []
        seen: set[str] = set()
        for u in links:
            if u in visited or u in seen:
                continue
            h = _host(u)
            if not h or host_count[h] >= _MAX_PER_HOST:
                continue
            seen.add(u)
            grade = F.source_grade(u)[0]
            path_kw = _keywords(urlparse(u).path.replace("-", " ").replace("/", " "))
            overlap = len(topic_kw & path_kw)
            scored.append((grade + 0.25 * min(overlap, 4), u))
        scored.sort(reverse=True)
        return [u for _, u in scored]

    # ------------------------------------------------------------------ #
    # 3. INDEX (chunk + embed + select)
    # ------------------------------------------------------------------ #
    def _chunk_sources(self, sources: list[dict]) -> list[dict]:
        chunks: list[dict] = []
        for si, s in enumerate(sources):
            for piece in _split_text(s["text"], RESEARCH_CHUNK_CHARS):
                chunks.append({"text": piece, "src": si, "url": s["url"],
                               "title": s["title"], "grade": s["grade"], "label": s["label"]})
        return chunks

    async def _select_chunks(self, topic: str, questions: list[str],
                             chunks: list[dict]) -> list[dict]:
        if not chunks:
            return []
        if len(chunks) <= RESEARCH_SYNTH_CHUNKS:
            return self._diversify(chunks, len(chunks))
        queries = [topic] + questions
        picked = await self._semantic_pick(queries, chunks)
        if picked is None:
            picked = self._lexical_pick(queries, chunks)
        return picked

    async def _semantic_pick(self, queries: list[str], chunks: list[dict]):
        """Rank chunks by max cosine similarity to any query (local MiniLM + FAISS). Returns None if
        the embedder isn't available so the caller can fall back to lexical selection."""
        try:
            from app.services.memory.embeddings import get_embedder
            import faiss
            embedder = get_embedder()
            if not await asyncio.to_thread(embedder.available):
                return None
            texts = [c["text"] for c in chunks]
            mat = await asyncio.to_thread(embedder.encode, texts)
            qmat = await asyncio.to_thread(embedder.encode, queries)
            index = faiss.IndexFlatIP(mat.shape[1])
            index.add(mat)
            k = min(len(chunks), RESEARCH_SYNTH_CHUNKS * 2)
            sims, idxs = index.search(qmat, k)
            best: dict[int, float] = {}
            for qi in range(len(queries)):
                for rank in range(k):
                    ci = int(idxs[qi][rank])
                    if ci < 0:
                        continue
                    score = float(sims[qi][rank]) + 0.15 * chunks[ci]["grade"]
                    if ci not in best or score > best[ci]:
                        best[ci] = score
            order = sorted(best, key=lambda ci: best[ci], reverse=True)
            ranked = [chunks[ci] for ci in order]
            return self._diversify(ranked, RESEARCH_SYNTH_CHUNKS)
        except Exception as e:  # noqa: BLE001
            logger.warning("semantic chunk pick failed (%s) — lexical fallback", type(e).__name__)
            return None

    def _lexical_pick(self, queries: list[str], chunks: list[dict]) -> list[dict]:
        qkw = _keywords(" ".join(queries))
        scored = []
        for c in chunks:
            overlap = len(qkw & _keywords(c["text"]))
            scored.append((overlap + 0.5 * c["grade"], c))
        scored.sort(key=lambda t: t[0], reverse=True)
        return self._diversify([c for _, c in scored], RESEARCH_SYNTH_CHUNKS)

    @staticmethod
    def _diversify(ranked: list[dict], limit: int, per_source: int = 4) -> list[dict]:
        """Take the top `limit` chunks but cap how many come from any single source, so one long page
        can't crowd out the others. Falls back to filling from the remainder if the cap is too tight."""
        out, used, seen = [], Counter(), set()
        for c in ranked:
            if used[c["src"]] < per_source:
                out.append(c)
                seen.add(id(c))
                used[c["src"]] += 1
            if len(out) >= limit:
                return out
        for c in ranked:        # top up if diversity cap left us short (dedup by IDENTITY, not value —
            if id(c) not in seen:   # two chunks with identical text are still distinct objects to keep)
                out.append(c)
                seen.add(id(c))
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------------ #
    # 4. SYNTHESIZE
    # ------------------------------------------------------------------ #
    async def _synthesize(self, topic: str, questions: list[str], top: list[dict],
                          sources: list[dict]) -> dict:
        # Number sources stably so the model can cite [n]; only sources that actually contributed a
        # selected chunk are offered (and get listed at the end).
        used_src_order: list[int] = []
        for c in top:
            if c["src"] not in used_src_order:
                used_src_order.append(c["src"])
        num_by_src = {si: i + 1 for i, si in enumerate(used_src_order)}

        ctx_parts = []
        for c in top:
            n = num_by_src[c["src"]]
            ctx_parts.append(f"[{n}] ({c['label']}, {_host(c['url'])})\n{c['text']}")
        context = "\n\n".join(ctx_parts)

        prompt = (
            f"You are a meticulous research analyst. Using ONLY the numbered sources below, write a "
            f"briefing on:\n\nTOPIC: {topic}\n\n"
            "Rules: rely only on the sources; cite the source number(s) like [2] after each claim; if "
            "the sources disagree, surface it; do NOT invent facts, numbers, or sources; weight "
            "authoritative sources over general ones. Be specific and concrete (names, numbers, dates).\n\n"
            "Return EXACTLY this structure and nothing else:\n"
            "SPOKEN: <2 to 4 sentences, plain spoken English, the single most important finding first — "
            "what a sharp aide would say out loud. No citations in this part.>\n"
            "---\n"
            "EXECUTIVE SUMMARY\n<3-5 sentences>\n\n"
            "KEY FINDINGS\n- <finding> [n]\n- <finding> [n]\n(5-8 bullets)\n\n"
            "CONTRADICTIONS / OPEN QUESTIONS\n- <point> (or write: None significant.)\n\n"
            "CONFIDENCE: <High|Medium|Low> — <one short reason tied to source quality/agreement>\n\n"
            f"SOURCES:\n{context}"
        )
        try:
            raw = await self.rotator.chat(
                [{"role": "system", "content": "You are a precise, source-grounded research analyst."},
                 {"role": "user", "content": prompt}],
                task="chat", temperature=0.3)
        except Exception as e:  # noqa: BLE001
            logger.warning("synthesis LLM failed (%s) — extractive fallback", type(e).__name__)
            return self._extractive(topic, top, sources, used_src_order, num_by_src)

        raw = (raw or "").strip()
        if not raw:
            return self._extractive(topic, top, sources, used_src_order, num_by_src)

        spoken, body = self._split_spoken(raw)
        conf = self._parse_confidence(body)
        src_list = self._format_sources(sources, used_src_order, num_by_src)
        full_md = f"# Briefing: {topic}\n\n{body}\n\n## SOURCES\n{src_list}"
        signature = self._signature(body)
        return {
            "topic": topic, "title": topic, "summary": spoken or _first_sentences(body, 3),
            "full_md": full_md, "confidence": conf, "signature": signature,
            "sources": [{"url": sources[si]["url"], "title": sources[si]["title"],
                         "label": sources[si]["label"], "n": num_by_src[si]} for si in used_src_order],
            "ok": True, "n_sources": len(used_src_order),
        }

    def _extractive(self, topic, top, sources, used_src_order, num_by_src) -> dict:
        """No-LLM fallback: a real, sourced (if shallow) summary from the top chunks, marked low
        confidence. Better an honest extract than a fabricated narrative or a hard failure."""
        lead = " ".join(_first_sentences(c["text"], 1) for c in top[:4]).strip()
        bullets = "\n".join(f"- {_first_sentences(c['text'], 2)} [{num_by_src[c['src']]}]"
                            for c in top[:6])
        src_list = self._format_sources(sources, used_src_order, num_by_src)
        full_md = (f"# Briefing: {topic}\n\n_(Assembled from sources without a synthesis pass — "
                   f"treat as a first cut.)_\n\nKEY POINTS\n{bullets}\n\n## SOURCES\n{src_list}")
        summary = (f"Here's a first cut on {topic}, sir — I gathered the sources but couldn't do a "
                   f"full synthesis pass just now. {_first_sentences(lead, 2)}").strip()
        return {"topic": topic, "title": topic, "summary": summary, "full_md": full_md,
                "confidence": "Low — assembled without a synthesis pass",
                "signature": self._signature(bullets),
                "sources": [{"url": sources[si]["url"], "title": sources[si]["title"],
                             "label": sources[si]["label"], "n": num_by_src[si]} for si in used_src_order],
                "ok": True, "n_sources": len(used_src_order)}

    # -- synthesis parsing helpers -- #
    @staticmethod
    def _split_spoken(raw: str) -> tuple[str, str]:
        # Prefer an explicit SPOKEN: ... --- delimiter.
        m = re.search(r"SPOKEN\s*:\s*(.+?)\n\s*-{3,}\s*\n(.*)", raw, re.DOTALL | re.IGNORECASE)
        if m:
            return _clean_spoken(m.group(1)), m.group(2).strip()
        m = re.search(r"SPOKEN\s*:\s*(.+?)(\n\n|\n#|\nEXECUTIVE)", raw, re.DOTALL | re.IGNORECASE)
        if m:
            spoken = _clean_spoken(m.group(1))
            # Keep the boundary token (e.g. "EXECUTIVE") in the body — start the body at the START
            # of the matched delimiter, not after it, so the section header isn't swallowed.
            body = raw[m.start(2):].strip() or raw
            return spoken, body
        return "", raw

    @staticmethod
    def _parse_confidence(body: str) -> str:
        m = re.search(r"CONFIDENCE\s*:\s*(.+)", body, re.IGNORECASE)
        if m:
            return m.group(1).strip().splitlines()[0][:160]
        return "Medium"

    @staticmethod
    def _format_sources(sources, used_src_order, num_by_src) -> str:
        lines = []
        for si in used_src_order:
            s = sources[si]
            lines.append(f"[{num_by_src[si]}] {s['title'] or _host(s['url'])} "
                         f"({s['label']}) — {s['url']}")
        return "\n".join(lines)

    @staticmethod
    def _signature(body: str) -> str:
        """A fingerprint of the substance (KEY FINDINGS if present, else the whole body), used to tell
        a re-run apart from the last one — material change detection for continuous monitoring."""
        m = re.search(r"KEY FINDINGS\s*(.+?)(?:CONTRADICTIONS|CONFIDENCE|## SOURCES|$)",
                      body, re.DOTALL | re.IGNORECASE)
        basis = (m.group(1) if m else body)
        norm = re.sub(r"[^a-z0-9 ]", " ", basis.lower())
        norm = " ".join(sorted(set(w for w in norm.split() if len(w) > 4)))
        return hashlib.sha1(norm.encode("utf-8")).hexdigest()

    @staticmethod
    def _empty(topic: str, reason: str) -> dict:
        return {"topic": topic, "title": topic,
                "summary": f"I dug into {topic}, sir, but {reason.lower()} I'd rather tell you that "
                           "plainly than guess. Want me to try again or narrow it down?",
                "full_md": f"# Briefing: {topic}\n\n{reason}", "confidence": "None",
                "signature": "", "sources": [], "ok": False, "n_sources": 0}


# --- module-level text helpers ------------------------------------------------------------------ #
def _split_text(text: str, size: int) -> list[str]:
    """Chunk on paragraph boundaries up to `size` chars; hard-split any giant paragraph."""
    text = (text or "").strip()
    if not text:
        return []
    paras = re.split(r"\n\s*\n", text)
    chunks, buf = [], ""
    for p in paras:
        p = p.strip()
        if not p:
            continue
        if len(p) > size:                      # one huge block — hard-split it
            if buf:
                chunks.append(buf)
                buf = ""
            for i in range(0, len(p), size):
                chunks.append(p[i:i + size])
            continue
        if len(buf) + len(p) + 2 <= size:
            buf = f"{buf}\n\n{p}" if buf else p
        else:
            if buf:
                chunks.append(buf)
            buf = p
    if buf:
        chunks.append(buf)
    return chunks


def _first_sentences(text: str, n: int) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", text)
    return " ".join(parts[:n]).strip()


def _clean_spoken(s: str) -> str:
    s = re.sub(r"\[\d+(?:\]\[\d+)*\]", "", s)   # drop any stray citations
    s = re.sub(r"\s+", " ", s).strip().strip('"')
    return s[:600]
