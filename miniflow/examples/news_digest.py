"""Morning news digest

Newsletter services read dozens of articles so you don't have to. This pipeline takes the ten stories on the Hacker News front page right now, downloads each article, works out how long it takes to read and what it is about, and puts together a "your morning reads" email, best stories first.

Run it:  miniflow example news_digest --ui
"""

from __future__ import annotations

import asyncio
import html
import re
import time
from collections import Counter
from typing import Any

from miniflow import task
from miniflow.examples._common import fetch_json, fetch_text, send_simulated

# Steps whose results best show what the run found (the dashboard's results card).
HIGHLIGHTS = ["build_digest", "send_digest"]

WATCH = (
    "Ten stories are looked up and ten article pages downloaded in parallel. Real websites "
    "are slow or flaky: watch for retries (orange) and timeouts. Article downloads are optional, so a site that never answers turns red but the digest still goes out. Each article is analysed on "
    "a CPU core the moment it arrives, and the digest waits for all ten."
)

STORIES = 10
TOP = "https://hacker-news.firebaseio.com/v0/topstories.json"
ITEM = "https://hacker-news.firebaseio.com/v0/item/{}.json"
WORDS_PER_MINUTE = 230
STOP = set(
    [
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "if",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "at",
        "by",
        "from",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "you",
        "your",
        "we",
        "our",
        "they",
        "their",
        "he",
        "she",
        "his",
        "her",
        "i",
        "me",
        "my",
        "not",
        "no",
        "so",
        "do",
        "does",
        "did",
        "can",
        "could",
        "will",
        "would",
        "should",
        "may",
        "might",
        "just",
        "than",
        "then",
        "there",
        "here",
        "what",
        "which",
        "who",
        "whom",
        "how",
        "when",
        "where",
        "why",
        "all",
        "any",
        "some",
        "more",
        "most",
        "other",
        "into",
        "over",
        "about",
        "up",
        "out",
        "also",
        "new",
        "one",
        "two",
        "has",
        "have",
        "had",
        "get",
        "got",
        "like",
        "make",
        "made",
        "use",
        "used",
        "using",
        "via",
        "per",
        "such",
        "only",
        "very",
        "much",
        "many",
        "each",
        "own",
        "same",
        "too",
        "s",
        "t",
        "don",
        "t",
        "ll",
        "re",
        "ve",
        "am",
        "pm",
        "via",
        "www",
        "com",
        "http",
        "https",
        "html",
        "amp",
        "nbsp",
    ]
)


@task(kind="io", retries=3, timeout=20)
def fetch_front_page() -> dict[str, Any]:
    """Get the list of stories on the Hacker News front page right now (official API)."""
    ids = fetch_json(TOP, snapshot="hn_top")
    return {
        "summary": f"{len(ids)} stories ranked right now; taking the top {STORIES}",
        "ids": ids[:STORIES],
    }


def fetch_story(front: dict[str, Any], *, rank: int) -> dict[str, Any]:
    item = fetch_json(ITEM.format(front["ids"][rank - 1]), snapshot=f"hn_story_{rank}")
    story = {
        "rank": rank, "title": item.get("title", "(untitled)"), "url": item.get("url"),
        "points": item.get("score", 0), "comments": item.get("descendants", 0),
        "text": item.get("text", ""), "hn": f"https://news.ycombinator.com/item?id={item.get('id')}",
    }  # fmt: skip
    return {"summary": f"#{rank} {story['title']} ({story['points']} points)", **story}


def fetch_article(story: dict[str, Any], *, rank: int) -> dict[str, Any]:
    if not story["url"]:  # "Ask HN" posts have their text on Hacker News itself
        return {
            "summary": "Discussion post: text taken from Hacker News",
            "page": story["text"] or "",
        }
    # Paywalls and bot blocks refuse for good (retrying won't help): use the headline only.
    page = fetch_text(story["url"], snapshot=f"hn_article_{rank}", timeout=12, on_refused="")
    if not page:
        return {"summary": "The site refused the download; using the headline only", "page": ""}
    return {
        "summary": f"Downloaded {len(page) / 1024:.0f} KB from {story['url'].split('/')[2]}",
        "page": page,
    }


def read_article(story: dict[str, Any], article: dict[str, Any] | None) -> dict[str, Any]:
    """Strip the web page down to its text, then estimate reading time and the main topics."""
    # article is None when its download failed for good (it is an optional step).
    page = article["page"] if article else ""
    raw = re.sub(r"(?is)<(script|style|nav|header|footer|svg)\b.*?</\1>", " ", page)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", raw))
    words = re.findall(r"[A-Za-z][A-Za-z'-]{2,}", text)
    minutes = max(1, round(len(words) / WORDS_PER_MINUTE)) if words else 0
    counts = Counter(w.lower() for w in words if w.lower() not in STOP)
    title_words = {w.lower() for w in re.findall(r"[A-Za-z]{3,}", story["title"])}
    topics = [w for w, _ in counts.most_common(40) if len(w) > 3][:5] or sorted(title_words)[:3]
    reading = f"{minutes} min read" if minutes else "article unavailable: headline only"
    return {
        "summary": f"\"{story['title']}\"\n{reading} · topics: {', '.join(topics) or '-'}",
        "title": story["title"], "url": story["url"] or story["hn"], "points": story["points"],
        "comments": story["comments"], "minutes": minutes, "topics": topics, "hn": story["hn"],
    }  # fmt: skip


# One branch per story: look it up, download the article (threads), read it (a CPU core).
# The tasks are module attributes so worker processes can find them by name.
for _r in range(1, STORIES + 1):
    globals()[f"story_{_r}"] = task(
        fetch_story, name=f"story_{_r}", kind="io", retries=2, timeout=15, after=["fetch_front_page"],
        kwargs={"rank": _r}, description=f"Look up story #{_r} on the front page: title, link, points and comments.",
    )  # fmt: skip
    globals()[f"article_{_r}"] = task(
        fetch_article, name=f"article_{_r}", kind="io", retries=2, timeout=25, after=[f"story_{_r}"],
        kwargs={"rank": _r}, optional=True,  # one slow website must not cancel the whole digest
        description=f"Download the web page that story #{_r} links to (optional: if the site is down, the digest still goes out).",
    )  # fmt: skip
    globals()[f"read_{_r}"] = task(
        read_article, name=f"read_{_r}", kind="cpu", after=[f"story_{_r}", f"article_{_r}"],
        description=f"Read story #{_r}'s article: reading time and main topics.",
    )  # fmt: skip


@task(kind="cpu", after=[f"read_{r}" for r in range(1, STORIES + 1)])
def build_digest(*reads: dict[str, Any]) -> dict[str, Any]:
    """Order the stories (most discussed first) and lay out the morning email."""
    ranked = sorted(reads, key=lambda r: r["points"] + 2 * r["comments"], reverse=True)
    total = sum(r["minutes"] for r in ranked)
    lines = [
        f"{i}. {r['title']} - {r['minutes'] or '?'} min · {r['points']} points, {r['comments']} comments"
        for i, r in enumerate(ranked, 1)
    ]
    body = "\n".join(f"{line}\n   {r['url']}" for line, r in zip(lines, ranked, strict=True))
    return {
        "summary": f"Your morning reads ({total} minutes in total):\n" + "\n".join(lines[:5]) + ("\n…" if len(lines) > 5 else ""),
        "email": f"Good morning! Today's top stories ({total} min of reading):\n\n{body}\n",
    }  # fmt: skip


@task(kind="async", after=[build_digest])
async def send_digest(digest: dict[str, Any]) -> dict[str, Any]:
    """Email the digest to subscribers (simulated: saved to the reports outbox)."""
    await asyncio.sleep(0.2)  # stands in for an email service call
    path = send_simulated("morning-digest", [digest["email"]])
    return {"summary": f"Digest sent at {time.strftime('%H:%M')} (simulated, saved to {path})"}
