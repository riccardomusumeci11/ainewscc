#!/usr/bin/env python3
"""Meanwhile — a daily, clustered AI/tech news digest for a side terminal pane.

Standalone by design: it runs in its OWN pane (e.g. a tmux split) next to Claude
Code and never reads or writes anything under ~/.claude.

Posture (see NOTICE): it aggregates publicly syndicated headlines + short
extracts + links from RSS/Atom feeds and key-free public APIs (arXiv, Hacker
News Algolia). It never fetches or shows article bodies. Every item is attributed
and linked back to its publisher.

Python 3, standard library only. No pip, no API key. Works offline from cache.
"""

import argparse
import hashlib
import html
import json
import os
import re
import select
import shutil
import subprocess
import sys
import termios
import textwrap
import threading
import time
import tty
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
FEEDS_FILE = os.path.join(DATA, "feeds.txt")
CONCEPTS_FILE = os.path.join(DATA, "concepts.txt")
CACHE_FILE = os.path.join(DATA, "news_cache.json")   # local view cache (news + concepts)
SRS_FILE = os.path.join(DATA, "srs_state.json")   # spaced-repetition schedule (personal)
SAVED_FILE = os.path.join(DATA, "saved.txt")      # things you press 's' to save (personal)
STAMP_FILE = os.path.join(HERE, ".last_refresh")

# Central feed: a daily-built, pre-summarized NEWS cache (no per-user key needed).
# A maintainer's CI builds dist/news_cache.json with their Gemini key; clients pull
# it. Override the source with NEWS_FEED_URL; build the published file via --build-news.
DIST_FILE = os.path.join(HERE, "dist", "news_cache.json")
REMOTE_URL = os.environ.get(
    "NEWS_FEED_URL",
    "https://raw.githubusercontent.com/riccardomusumeci11/meanwhile/dist/news_cache.json")

# CC-aware: a shared one-word state file ("WORKING"/"WAITING") written by Claude
# Code hooks and read by the panel. Lives outside the repo. Missing -> WORKING.
CC_STATE_DIR = os.path.expanduser("~/.cc-learn-banner")
CC_STATE_FILE = os.path.join(CC_STATE_DIR, "cc_state")
CC_POLL_SECS = 0.5     # how often the panel checks the state file (light, mtime-gated)
# Remember which pill we're on, so a restart (e.g. the tmux pane reopening) resumes
# from there instead of pill 1.
POS_FILE = os.path.join(CC_STATE_DIR, "panel_pos")

REFRESH_EVERY = 24 * 3600  # daily
EXTRACT_MAX = 700          # hard cap on the syndicated extract we display
MIN_POOL_EXTRACT = 80      # a pill needs THIS many chars of real prose to enter the pool
PER_FEED = 12              # items taken per source
PER_CHANGELOG = 2          # latest releases taken per changelog feed
MAX_AGE_DAYS = 60          # keep it a "news" digest: drop items older than this

# Spaced repetition: a concept becomes "due" again after a GROWING gap (days).
SRS_LADDER = [1, 3, 7, 21, 60]
# Rotation interleave: roughly one due concept per this many news pills.
CONCEPTS_PER_NEWS = 5
# Quality: a news pill needs a summary OR a real extract to be worth a glance, and
# the digest is capped to the top-ranked stories so the weak tail never shows.
MIN_PANEL_EXTRACT = 120
TOP_NEWS = 50


def worth_showing(cluster):
    """Keep concepts/releases; keep a news pill only if it has a summary or a real
    extract (not a bare title-only item)."""
    if cluster.get("kind") != "news":
        return True
    if cluster.get("summary"):
        return True
    return any(len(it.get("extract", "")) >= MIN_PANEL_EXTRACT
               for it in cluster.get("items", []))
# Dwell time scales with pill length (seconds): short pills sit briefly, long
# multi-source ones stay long enough to read. --interval sets the ceiling.
DWELL_MIN = 15

# Statusline mode (one line inside Claude Code): rotate over the top-signal pills
# on a TIME index so the line is stable within a window and never flickers when
# the command is re-invoked continuously.
STATUS_POOL = 40       # rotate only over the highest-priority pills
STATUS_WINDOW = 90     # seconds each pill stays put (60-120 is reasonable)
STATUS_MAXW = 400      # sanity ceiling only — never CLIP a genuinely wide terminal
STATUS_FALLBACK_W = 120  # last-resort width when the real one is unknown (never 80)

# ── Gemini summarization (DIGEST/news only; legal-by-design) ──────────────────
# Source of the summary is ONLY the RSS extract — never the article body. The key
# is read from the environment, never stored or committed. Free flash-lite model.
SUMMARY_CACHE_FILE = os.path.join(DATA, "summaries.json")  # persistent, gitignored
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")
GEMINI_ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/"
                   "models/{model}:generateContent")
GEMINI_PROMPT = (
    "You rewrite one AI-industry news item for a glanceable feed, using ONLY the "
    "headline and source extract provided below. Write a faithful, self-contained "
    "summary of 3 to 5 full sentences (use fewer ONLY if the source is genuinely "
    "thin). Rules:\n"
    "1) Lead with the concrete news stated in the HEADLINE and never lose or bury "
    "it — that specific fact is the point of the card.\n"
    "2) Preserve EVERY specific found in the source: names, numbers, dates, version "
    "numbers, benchmarks, money amounts, companies, places.\n"
    "3) Stay strictly factual and neutral. Do NOT add opinions, analysis, "
    "implications, predictions, or anything not stated in the source. Do NOT soften, "
    "hedge, or generalize the claim (e.g. never turn 'X outperforms Y' into 'X may "
    "be capable').\n"
    "4) Do not copy the original phrasing.\n"
    "Output only the summary, with no preamble or labels.")
GEMINI_MAX_CALLS = int(os.environ.get("GEMINI_MAX_CALLS", "60"))  # per refresh cap
# Free tier is ~15 requests/minute. Space calls to stay safely under it (~13 RPM)
# so a daily build summarizes EVERY card instead of bursting into a 429 wall.
GEMINI_SPACING = float(os.environ.get("GEMINI_SPACING", "4.5"))  # seconds between calls
GEMINI_FACTS_MAX = 700  # chars of RSS extract fed to the model
GEMINI_MAX_TOKENS = 380  # output budget — room for a fuller 3-5 sentence summary
SUMMARY_SPACING = GEMINI_SPACING   # seconds between summary calls (any provider)

# Free, OpenAI-compatible fallback summarizers, tried in order AFTER Gemini. Each is
# used only if its API key is present in the environment; with no keys set at all,
# pills simply fall back to the raw RSS extract. (name, env var, endpoint, model)
OPENAI_COMPAT_PROVIDERS = [
    ("groq", "GROQ_API_KEY", "https://api.groq.com/openai/v1/chat/completions",
     os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")),
    ("cerebras", "CEREBRAS_API_KEY", "https://api.cerebras.ai/v1/chat/completions",
     os.environ.get("CEREBRAS_MODEL", "llama-3.3-70b")),
]

# The Verge has only a site-wide RSS; keep just AI/ML items from THAT feed.
# Site-wide feeds with no AI-only edition: keep only the AI/ML items from THESE
# hosts (scoping, not importance-filtering). Every dedicated AI feed is kept whole.
GENERAL_FEED_HOSTS = ("theverge.com", "technologyreview.com")
VERGE_HOST = "theverge.com"

# Source authority tiers (0..1) for ranking: official lab > press > single voice.
# arXiv is deliberately LOW so papers never top the digest.
_LAB_HOSTS = ("openai.com", "research.google", "deepmind.google",
              "mistral.ai", "huggingface.co")
_PRESS_HOSTS = ("the-decoder.com", "techcrunch.com", "venturebeat.com",
                "technologyreview.com", "theverge.com")
_VOICE_HOSTS = ("simonwillison.net", "sebastianraschka.com", "thegradient.pub")
USER_AGENT = (
    "meanwhile/1.0 (standalone personal news reader; "
    "https://github.com/; respects robots & rate limits)"
)

# ── ANSI ────────────────────────────────────────────────────────────────────
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
HIDE_CUR = "\033[?25l"
SHOW_CUR = "\033[?25h"
CLEAR = "\033[2J\033[H"
ACCENT = "\033[38;5;81m"    # cyan border
HEAD = "\033[38;5;222m"     # yellow headline
SRC = "\033[38;5;156m"      # green source
META = "\033[38;5;245m"     # grey meta
LINK = "\033[38;5;111m"     # blue link

# ── dark theme: the pane paints its OWN black background so it looks right on
#    any terminal (incl. a light one). Foregrounds chosen for black bg. ──
PANE_BG = "\033[48;5;232m"           # near-black pane background
D_TITLE = BOLD + "\033[38;5;231m"    # big title: bold bright white
D_RULE = "\033[38;5;239m"            # thin underline rule under the title
D_SUMMARY = "\033[38;5;253m"         # the short summary: bright
D_SUB = BOLD + "\033[38;5;117m"      # sub-title (▸ …): bold light blue
D_BODY = "\033[38;5;250m"            # the longer explanation: light grey
D_META = "\033[38;5;245m"            # source · date: grey
D_LINK = "\033[38;5;80m"             # link: cyan
D_TAG = BOLD + "\033[38;5;215m"      # header type tag: warm
D_HINT = "\033[38;5;244m"            # header counter + footer legend


def _domain(url):
    host = urllib.parse.urlparse(url or "").netloc.lower()
    return host[4:] if host.startswith("www.") else host

STOPWORDS = {
    # grammar / function words
    "the", "a", "an", "and", "or", "but", "for", "to", "of", "in", "on", "at",
    "by", "with", "from", "as", "is", "are", "was", "be", "been", "it", "its",
    "this", "that", "these", "those", "how", "why", "what", "who", "your", "when",
    "you", "we", "our", "can", "will", "now", "out", "up", "via", "into", "not",
    "no", "do", "does", "has", "have", "they", "their", "about", "more", "most",
    "using", "use", "uses", "vs", "get", "gets", "may", "could", "would", "should",
    "after", "over", "than", "just", "all", "one", "two", "way", "ways", "its",
    "here", "his", "her", "them", "been", "being", "also", "some", "any", "each",
    # generic AI / tech filler that otherwise causes false topic merges
    "ai", "llm", "llms", "model", "models", "large", "language", "languages",
    "new", "open", "source", "data", "system", "systems", "tool", "tools",
    "app", "apps", "feature", "features", "platform", "tech", "technology",
    "startup", "company", "companies", "business", "user", "users", "team",
    "memory", "online", "work", "working", "course", "courses", "next", "era",
    # ML/LLM domain filler — these recur across unrelated stories and must NOT
    # drive clustering (only proper nouns / model names should).
    "inference", "serving", "serve", "context", "token", "tokens", "scaling",
    "scale", "training", "train", "benchmark", "benchmarks", "benchmarking",
    "performance", "efficient", "efficiency", "coding", "code", "agent",
    "agents", "agentic", "reasoning", "multimodal", "multimodality",
    "foundational", "foundation", "paper", "papers", "dataset", "datasets",
    "compute", "gpu", "gpus", "neural", "deep", "fine", "tuning", "prompt",
    "prompts", "generative", "embedding", "embeddings", "transformer",
    # generic news verbs/nouns
    "news", "latest", "today", "week", "year", "report", "reports", "study",
    "studies", "launch", "launches", "launched", "release", "released", "releases",
    "announce", "announces", "announced", "ban", "bans", "banned", "update",
    "updates", "plan", "plans", "build", "building", "built", "make", "makes",
    "making", "help", "helps", "helping", "look", "looks", "looking", "set",
    "says", "said", "show", "shows", "showing", "first", "top", "best", "big",
    "free", "real", "into", "across", "amid", "while", "still", "back",
}


# ── source parsing ──────────────────────────────────────────────────────────
def read_sources():
    """Return list of source spec strings from feeds.txt."""
    sources = []
    try:
        with open(FEEDS_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    sources.append(line)
    except FileNotFoundError:
        pass
    return sources


# ── HTTP ────────────────────────────────────────────────────────────────────
class SkipSource(Exception):
    """Raised to skip a source (dead, blocked, throttled)."""


def http_get(url, timeout=20, _redirects=2):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        # Polite: on rate-limit / forbidden, back off and skip — do not retry.
        if e.code in (403, 429):
            raise SkipSource(f"HTTP {e.code} (backing off)") from e
        # Follow permanent/temporary redirects urllib doesn't auto-follow (307/308).
        if e.code in (301, 302, 307, 308) and _redirects > 0:
            loc = e.headers.get("Location")
            if loc:
                return http_get(urllib.parse.urljoin(url, loc), timeout, _redirects - 1)
        raise SkipSource(f"HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SkipSource(e.__class__.__name__) from e


# ── cleaning helpers ────────────────────────────────────────────────────────
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# remove <style>/<script> blocks WITH their content (CSS/JS leaks into summaries)
_BLOCK_RE = re.compile(r"<(style|script)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
# remove leftover CSS rule fragments like ".modal { display:none; }"
_CSS_RE = re.compile(r"[.#]?[\w-]+\s*\{[^}]*\}")


def strip_html(raw):
    txt = html.unescape(raw or "")
    txt = _BLOCK_RE.sub(" ", txt)
    txt = _TAG_RE.sub(" ", txt)
    txt = html.unescape(txt)
    txt = _CSS_RE.sub(" ", txt)
    return _WS_RE.sub(" ", txt).strip()


# RSS footer boilerplate that publishers append to every item — pure noise.
_FOOTER_RE = re.compile(
    r"\s*(the (post|article|story)\b.*?\bappeared first on\b.*$"
    r"|\bappeared first on\b.*$"
    r"|\b(read more|continue reading|read the (full )?(article|story|post))\b.*$"
    r"|\[\s*…\s*\]\s*$|\[\.\.\.\]\s*$)",
    re.I | re.S)


def make_extract(raw):
    """Clean + hard-cap the publisher's syndicated summary. Never article body."""
    txt = strip_html(raw)
    # If it still looks like leftover markup/CSS/JS, drop it rather than show junk.
    if "{" in txt and "}" in txt:
        return ""
    txt = _FOOTER_RE.sub("", txt).strip()   # drop "appeared first on …" / "read more"
    if len(txt) <= EXTRACT_MAX:
        return txt
    cut = txt[:EXTRACT_MAX]
    # prefer to end on a sentence, else on a word boundary
    dot = cut.rfind(". ")
    if dot >= EXTRACT_MAX * 0.5:
        return cut[: dot + 1]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > 0 else cut).rstrip() + "…"


# Teaser/clickbait phrases — an extract built around these carries no information.
_TEASERS = (
    "what's your", "what is your", "whats your", "here's what", "heres what",
    "find out", "you won't believe", "read more", "continue reading",
    "click here", "sign up", "subscribe", "keep reading",
)
MIN_EXTRACT = 45  # below this, a "summary" is a label/teaser, not a description


def good_extract(extract, title):
    """Return a clean informative extract, or "" to fall back to the title.

    Drops teasers, bare short questions, title-duplicates and too-short blurbs —
    better to show only the title than an extract that confuses (defect #4)."""
    e = (extract or "").strip()
    if len(e) < MIN_EXTRACT:
        return ""
    low = _WS_RE.sub(" ", e.lower())
    if low == _WS_RE.sub(" ", title.lower()):  # description == title, no value
        return ""
    if e.rstrip().endswith("?") and len(e) < 120:  # short rhetorical teaser
        return ""
    if any(p in low for p in _TEASERS):
        return ""
    return e


# Pure metadata masquerading as text (e.g. "▲ 142 points · 30 comments").
_METADATA_RE = re.compile(
    r"^\s*[▲△▶]?\s*\d[\d,]*\s+(points?|comments?|upvotes?|votes?)\b", re.I)


def has_substance(item):
    """Pool gate: keep an item ONLY if it carries a real, informative extract —
    at least MIN_POOL_EXTRACT chars of prose, not metadata. Nude title-only pills
    (e.g. Hacker News) and one-liners are dropped rather than shown. Concepts are
    exempt: their dense sentence lives in the headline, not in `extract`."""
    if item.get("kind") == "concept":
        return True
    e = (item.get("extract") or "").strip()
    if len(e) < MIN_POOL_EXTRACT:
        return False
    if _METADATA_RE.search(e):
        return False
    if " " not in e:                 # a single long token is not prose
        return False
    return True


# AI/ML relevance for free-text sources (Hacker News): keep only on-topic items.
_AI_RE = re.compile(
    r"\b(a\.?i|llm|llms|gpt|gpts|nlp|rag|moe|agent|agents|agentic|model|models|"
    r"modeling|modelling|neural|inference|embedding|embeddings|transformer|"
    r"transformers|diffusion|chatbot|chatgpt|copilot|prompt|prompts|generative|"
    r"multimodal|reasoning|openai|anthropic|deepmind|gemini|claude|llama|mistral|"
    r"qwen|deepseek|minimax|kimi|mixtral|gemma|nemotron|huggingface)\b", re.I)
_AI_PHRASES = (
    "language model", "machine learning", "deep learning", "hugging face",
    "large language", "foundation model", "mixture of experts", "stable diffusion",
    "neural network", "fine-tun", "text-to-", "chain of thought",
)


def is_ai_relevant(title, url):
    blob = f"{title} {url}".lower()
    return bool(_AI_RE.search(blob)) or any(p in blob for p in _AI_PHRASES)


# Commerce / promo junk: shopping-deal posts and event ads that ride in on a feed
# (often with "AI" buried in product copy) and have zero news value. Tuned to NOT
# catch real business stories like "strikes a multi-year deal" or "$650M raise".
_PROMO_RE = re.compile(
    r"\b(prime day|black friday|cyber monday|deal of the day|"
    r"best .{0,40}?deals?\b|deals?\b.{0,20}?(on|available|under|during)\b|"
    r"save up to|save \$|\d+% off|\$\d+ off|discount code|coupon|on sale|"
    r"early[- ]bird|founder summit|days? left to save|last chance to save|"
    r"register (now|today)|get your tickets?|buy (now|one))\b", re.I)
# Daily newsletter wrappers: low-signal roundups that recur every day.
_NEWSLETTER_RE = re.compile(r"^\s*(the download|the algorithm|the briefing)\b", re.I)


def is_promo_junk(title, url=""):
    """True for shopping-deal / event-ad / daily-newsletter-roundup items — never
    news, regardless of source. Applied to every feed before clustering."""
    return bool(_PROMO_RE.search(title) or _NEWSLETTER_RE.search(title))


def parse_date(text):
    if not text:
        return ""
    text = text.strip()
    try:
        dt = parsedate_to_datetime(text)
        if dt:
            return dt.astimezone(timezone.utc).date().isoformat()
    except (TypeError, ValueError, IndexError):
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.date().isoformat()
    except ValueError:
        return text[:10] if re.match(r"\d{4}-\d{2}-\d{2}", text) else ""


def _local(tag):
    return tag.split("}")[-1]


def _child(elem, *names):
    for ch in elem:
        if _local(ch.tag) in names:
            return ch
    return None


# ── source fetchers → list of normalized items ──────────────────────────────
def norm(title, source, date, raw_summary, url, note="", kind="news"):
    """Normalize one item. `raw_summary` is the publisher's syndicated summary;
    it is cleaned, capped, and sanitized (or dropped) here. `note` is a small
    relevance hint (e.g. HN points) shown separately — never faked as a summary.
    `kind` is one of: "news", "changelog", "concept"."""
    t = strip_html(title)[:300]
    return {
        "title": t,
        "source": source,
        "date": date,
        "extract": good_extract(make_extract(raw_summary), t),
        "note": note,
        "url": url,
        "kind": kind,
    }


def is_arxiv_item(item):
    return (item.get("source") or "").startswith("arXiv")


def authority(source, url, kind):
    """0..1 authority for ranking: official lab > press > single voice; arXiv low."""
    if kind == "changelog":
        return 1.0          # the canonical release feed of a tool you run
    host = urllib.parse.urlparse(url or "").netloc.lower()
    if "arxiv" in host or (source or "").startswith("arXiv"):
        return 0.2          # primary research, deliberately low (never tops the digest)
    if any(h in host for h in _LAB_HOSTS):
        return 1.0          # first-party AI lab
    if any(h in host for h in _PRESS_HOSTS):
        return 0.6          # reputable press
    if any(h in host for h in _VOICE_HOSTS):
        return 0.4          # single technical voice
    return 0.5


# A concrete launch (model/tool/feature/release) is the highest-value AI signal.
_LAUNCH_RE = re.compile(
    r"\b(launch(?:e[ds]|ing)?|releas(?:e[ds]|ing)?|introduc(?:e[ds]|ing)?|"
    r"announc(?:e[ds]|ing)?|unveil(?:e[ds]|ing)?|debut(?:e[ds]|ing)?|"
    r"ship(?:s|ped|ping)?|roll(?:s|ed|ing)? out|open[- ]?sourc(?:e[ds]|ing)?|"
    r"now available|available now|new (?:model|tool|feature|release|version|app|api))\b",
    re.I)


def is_launch(title, extract):
    return bool(_LAUNCH_RE.search(f"{title} {extract}"))


# Low-substance vendor/PR phrasing: "company X adopts/brings/uses AI" customer
# stories that carry no concrete development. Kept, but ranked DOWN so they don't
# crowd out real launches and research. Tuned to spare genuine stories ("how GPT
# helped solve…", real partnerships) by matching only adoption/marketing shapes.
_PR_FLUFF_RE = re.compile(
    r"(\bbrings\b.{0,40}?\bto\b|\bto employees\b|\bis building the future\b|"
    r"\bhow\b.{0,40}?\bis (building|using)\b|\bcomes to\b|"
    r"\b(rolls?|rolled) out\b.{0,60}?\b(course|training)\b)", re.I)


def is_pr_fluff(title):
    return bool(_PR_FLUFF_RE.search(title))


def fetch_rss(url):
    raw = http_get(url)
    root = ET.fromstring(raw)  # ParseError -> caller skips
    channel = _child(root, "channel") or root
    st = _child(channel, "title")
    source = strip_html(st.text if st is not None else "") or urllib.parse.urlparse(url).netloc
    source = source.split(" | ")[0].split(" - ")[0].strip()[:48]

    items = []
    for elem in root.iter():
        if _local(elem.tag) not in ("item", "entry"):
            continue
        t = _child(elem, "title")
        title = strip_html(t.text if t is not None else "")
        if not title:
            continue
        # link: RSS <link> text, or Atom <link href=...>
        link = ""
        le = _child(elem, "link")
        if le is not None:
            link = (le.text or "").strip() or le.get("href", "")
        if not link:
            g = _child(elem, "guid", "id")
            if g is not None and (g.text or "").startswith("http"):
                link = g.text.strip()
        # extract: ONLY description/summary — NEVER content:encoded / content
        d = _child(elem, "description", "summary", "subtitle")
        raw_summary = d.text if d is not None else ""
        dt = _child(elem, "pubDate", "published", "updated", "date")
        date = parse_date(dt.text if dt is not None else "")
        items.append(norm(title, source, date, raw_summary, link))
        if len(items) >= PER_FEED:
            break
    return items


def fetch_arxiv(category):
    q = urllib.parse.urlencode({
        "search_query": f"cat:{category}",
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": PER_FEED,
    })
    raw = http_get("http://export.arxiv.org/api/query?" + q)
    root = ET.fromstring(raw)
    items = []
    for elem in root.iter():
        if _local(elem.tag) != "entry":
            continue
        t = _child(elem, "title")
        title = strip_html(t.text if t is not None else "")
        s = _child(elem, "summary")
        raw_summary = s.text if s is not None else ""
        dt = _child(elem, "published", "updated")
        date = parse_date(dt.text if dt is not None else "")
        link = ""
        idel = _child(elem, "id")
        if idel is not None:
            link = (idel.text or "").strip()
        items.append(norm(title, f"arXiv {category}", date, raw_summary, link))
    return items


def fetch_hn(query):
    params = urllib.parse.urlencode({
        "query": query,
        "tags": "story",
        "numericFilters": "points>50",
        "hitsPerPage": PER_FEED,
    })
    raw = http_get("https://hn.algolia.com/api/v1/search_by_date?" + params)
    data = json.loads(raw.decode("utf-8"))
    items = []
    for h in data.get("hits", []):
        title = strip_html(h.get("title") or "")
        if not title:
            continue
        url = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        # Defect #3: HN full-text search drags in off-topic stories — keep only
        # items whose title/url are clearly about AI/ML.
        if not is_ai_relevant(title, url):
            continue
        date = (h.get("created_at") or "")[:10]
        pts = h.get("points", 0)
        ncom = h.get("num_comments", 0)
        # HN has no summary. Defect #2: do NOT fake an extract from metadata.
        # The title is the content; points/comments are a small relevance note.
        note = f"▲ {pts} points · {ncom} comments on Hacker News"
        items.append(norm(title, "Hacker News", date, "", url, note=note))
    return items


def _repo_name(url):
    """github.com/<owner>/<repo>/releases.atom -> '<repo>'."""
    parts = urllib.parse.urlparse(url).path.strip("/").split("/")
    return parts[1] if len(parts) >= 2 else (parts[0] if parts else url)


def fetch_changelog(url):
    """A release/changelog Atom feed (e.g. GitHub <repo>/releases.atom or
    tags.atom). Each recent release becomes a high-signal CHANGELOG item:
    repo + version as the title, the short release-note extract, and the link."""
    raw = http_get(url)
    root = ET.fromstring(raw)  # ParseError -> caller skips
    repo = _repo_name(url)
    items = []
    for elem in root.iter():
        if _local(elem.tag) != "entry":
            continue
        t = _child(elem, "title")
        version = strip_html(t.text if t is not None else "").strip()
        if not version:
            continue
        link = ""
        le = _child(elem, "link")
        if le is not None:
            link = (le.text or "").strip() or le.get("href", "")
        if not link:
            idel = _child(elem, "id")
            if idel is not None and (idel.text or "").startswith("http"):
                link = idel.text.strip()
        c = _child(elem, "content", "summary")
        raw_summary = c.text if c is not None else ""
        dt = _child(elem, "updated", "published")
        date = parse_date(dt.text if dt is not None else "")
        title = f"{repo} {version}" if version.lower() not in repo.lower() else version
        items.append(norm(title, repo, date, raw_summary, link,
                          note="release / changelog", kind="changelog"))
        if len(items) >= PER_CHANGELOG:
            break
    return items


def fetch_source(spec):
    if spec.startswith("arxiv:"):
        return fetch_arxiv(spec.split(":", 1)[1].strip())
    if spec.startswith("hn:"):
        return fetch_hn(spec.split(":", 1)[1].strip().strip('"'))
    if spec.startswith("changelog:"):
        return fetch_changelog(spec.split(":", 1)[1].strip())
    if spec.startswith(("http://", "https://")):
        return fetch_rss(spec)
    raise SkipSource("unknown source type")


# ── clustering (Jaccard on significant title tokens, stdlib only) ────────────
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[.\-+][a-z0-9]+)*")


def sig_tokens(title):
    toks = _TOKEN_RE.findall(title.lower())
    out = set()
    for t in toks:
        if t in STOPWORDS:
            continue
        if t.isdigit():
            continue
        if len(t) < 3:
            continue
        out.add(t)
    return out


class DSU:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def cluster_items(items):
    """Group items about the same topic.

    Tuned to AVOID the transitive-closure collapse (everything chaining into one
    giant cluster through a hub word): two items merge only when they share at
    least TWO reasonably-distinctive title tokens, or are near-duplicate titles.
    A single shared token never merges — that's what caused runaway chains."""
    n = len(items)
    toks = [sig_tokens(it["title"]) for it in items]

    # document frequency: how many items each token appears in
    df = {}
    for ts in toks:
        for t in ts:
            df[t] = df.get(t, 0) + 1
    # ignore tokens that are too common this batch (residual filler)
    common_cutoff = max(3, int(0.06 * n))
    src = [it["source"].lower() for it in items]

    def strong(i, j):
        shared = toks[i] & toks[j]
        if not shared:
            return False
        union = toks[i] | toks[j]
        jacc = len(shared) / len(union)
        # Clustering groups the SAME story across DIFFERENT outlets. Two items from
        # the SAME source are almost always distinct stories that merely share
        # boilerplate title words ("Introducing X" / "Introducing Y") — never merge
        # them unless the titles are near-identical (a genuine re-post).
        if src[i] == src[j]:
            return jacc >= 0.85
        # Require overlap on DISTINCTIVE tokens (proper nouns / model names like
        # "minimax-m3", "gemini", "fable"), not common words. Two such shared
        # tokens = same specific topic — BUT also demand real overall title overlap,
        # otherwise org hub-words ("openai", "google", "deepmind", "anthropic")
        # transitively chain unrelated stories into one giant wrong cluster.
        distinctive = [t for t in shared if df[t] <= common_cutoff]
        if len(distinctive) >= 2 and jacc >= 0.25:
            return True
        # near-duplicate title (same story re-syndicated across outlets)
        return jacc >= 0.6

    dsu = DSU(n)
    for i in range(n):
        if not toks[i]:
            continue
        for j in range(i + 1, n):
            if toks[j] and strong(i, j):
                dsu.union(i, j)

    groups = {}
    for i in range(n):
        groups.setdefault(dsu.find(i), []).append(items[i])
    return list(groups.values())


def dedupe_group(group):
    """Drop near-identical items (same source+title, or identical url)."""
    seen, out = set(), []
    for it in sorted(group, key=lambda x: x["date"], reverse=True):
        key = (it["source"].lower(), it["title"].lower())
        if key in seen or (it["url"] and it["url"] in seen):
            continue
        seen.add(key)
        if it["url"]:
            seen.add(it["url"])
        out.append(it)
    return out


def build_clusters(items):
    clusters = []
    for group in cluster_items(items):
        group = dedupe_group(group)
        sources = {it["source"] for it in group}
        dates = [it["date"] for it in group if it["date"]]
        latest = max(dates) if dates else ""
        # headline = most recent item's title
        newest = sorted(group, key=lambda x: x["date"], reverse=True)[0]
        # a cluster's kind = CHANGELOG if any release is in it, else news
        kind = "changelog" if any(it.get("kind") == "changelog" for it in group) else "news"
        auth = max(authority(it["source"], it["url"], it.get("kind", "news"))
                   for it in group)
        clusters.append({
            "headline": newest["title"],
            "latest": latest,
            "n_sources": len(sources),
            "kind": kind,
            "authority": round(auth, 3),
            "launch": any(is_launch(it["title"], it.get("extract", "")) for it in group),
            "has_extract": any(it.get("extract") for it in group),
            "is_arxiv": any(is_arxiv_item(it) for it in group),
            "pr_fluff": is_pr_fluff(newest["title"]),
            "items": group,
        })

    today = datetime.now(timezone.utc).date()

    def score(c):
        # Four OBJECTIVE signals of "importance in the AI world" (not user-tuned).
        launch = 1.0 if (c["launch"] or c["kind"] == "changelog") else 0.0
        size = min(c["n_sources"], 4) / 4.0
        try:
            age = (today - datetime.fromisoformat(c["latest"]).date()).days
        except (ValueError, TypeError):
            age = 14
        recency = max(0, 30 - age) / 30.0
        s = (0.34 * launch + 0.22 * size + 0.20 * recency + 0.24 * c["authority"])
        # Keep-all, just rank: weak items sink, never vanish.
        if not c["has_extract"]:
            s *= 0.5            # title-only feeds (research.google, huggingface) rank LOW
        if c["is_arxiv"]:
            s = min(s, 0.40)    # papers are kept but never top the digest
        if c.get("pr_fluff") and c["n_sources"] < 2:
            s *= 0.6            # single-source vendor/PR "adopts AI" stories sink
        return s

    for c in clusters:
        c["score"] = round(score(c), 4)
    clusters.sort(key=lambda c: (c["score"], c["latest"]), reverse=True)
    return clusters


# ── evergreen concepts + spaced repetition ──────────────────────────────────
def _concept_key(text):
    """Stable short id for a concept line (survives reordering of the file)."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def load_concepts():
    """Return [(key, category, text)] from concepts.txt. Missing file -> []."""
    out = []
    try:
        with open(CONCEPTS_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                cat = line.split("│", 1)[0].strip() if "│" in line else "CONCEPT"
                out.append((_concept_key(line), cat, line))
    except FileNotFoundError:
        pass
    return out


def load_srs():
    try:
        with open(SRS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_srs(state):
    try:
        with open(SRS_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
    except OSError:
        pass


def _next_interval(cur):
    """Growing gap ladder: 0 -> 1 -> 3 -> 7 -> 21 -> 60 (days), then cap."""
    for g in SRS_LADDER:
        if g > cur:
            return g
    return SRS_LADDER[-1]


def select_due_concepts(quota, now=None):
    """Pick up to `quota` concepts that are DUE for review (never shown, or last
    shown longer ago than their current interval), advancing each one's schedule.

    Returns a list of concept "pills". Side effect: stamps srs_state.json so the
    chosen concepts won't resurface until their (now longer) interval elapses."""
    if quota <= 0:
        return []
    now = now if now is not None else now_ts()
    concepts = load_concepts()
    if not concepts:
        return []
    state = load_srs()

    def due_since(key):
        st = state.get(key)
        if not st:
            return float("inf")            # never shown -> most overdue
        return (now - st.get("last", 0)) - st.get("interval", 1) * 86400

    due = [(k, c, t) for (k, c, t) in concepts if due_since(k) >= 0]
    # most-overdue first; stable for never-shown via file order
    due.sort(key=lambda kct: due_since(kct[0]), reverse=True)

    chosen = []
    for key, cat, text in due[:quota]:
        cur = state.get(key, {}).get("interval", 0)
        state[key] = {"last": now, "interval": _next_interval(cur), "text": text}
        chosen.append(concept_pill(key, cat, text))
    if chosen:
        save_srs(state)
    return chosen


def concept_pill(key, category, text):
    """A concept rendered as a cluster-shaped pill so the viewer treats it uniformly."""
    return {
        "headline": text,
        "latest": "",
        "n_sources": 0,
        "kind": "concept",
        "category": category,
        "authority": 1.0,
        "score": 0.0,
        "items": [],
    }


def interleave_concepts(clusters, concepts):
    """Keep news/releases up front by priority; sprinkle due concepts in at a
    steady cadence (~1 every CONCEPTS_PER_NEWS pills), never as the first pill."""
    if not concepts:
        return clusters
    out, ci = [], 0
    for i, c in enumerate(clusters):
        out.append(c)
        # after every Nth news pill, drop in one due concept (if any remain)
        if (i + 1) % CONCEPTS_PER_NEWS == 0 and ci < len(concepts):
            out.append(concepts[ci])
            ci += 1
    out.extend(concepts[ci:])   # any leftovers ride along at the end
    return out


# ── LLM summarization (DIGEST only; from the RSS extract, never the article) ──
# Gemini is primary; Groq/Cerebras are free OpenAI-compatible fallbacks (see
# OPENAI_COMPAT_PROVIDERS). On one provider's quota the run drops it and tries the
# next; if all are exhausted or absent, the pill keeps its raw RSS extract.
class ProviderQuota(Exception):
    """Raised on 429/quota so the run drops THIS provider and tries the next."""


GeminiQuota = ProviderQuota   # back-compat alias


def _summary_key(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def load_summaries():
    try:
        with open(SUMMARY_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_summaries(cache):
    try:
        with open(SUMMARY_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def gemini_summarize(facts, key):
    """Rewrite RSS-extract facts in new words via Gemini flash-lite (REST/urllib).
    Returns a short summary, or None on any non-quota failure. Raises ProviderQuota
    on 429 so the caller can drop the provider and fall back."""
    body = json.dumps({
        "contents": [{"parts": [{"text": f"{GEMINI_PROMPT}\n\n{facts}"}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": GEMINI_MAX_TOKENS},
    }).encode("utf-8")
    url = (GEMINI_ENDPOINT.format(model=GEMINI_MODEL)
           + "?key=" + urllib.parse.quote(key))
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            out = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429 or (e.code == 403 and b"quota" in (e.read() or b"").lower()):
            raise ProviderQuota() from e
        return None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    try:
        parts = out["candidates"][0]["content"]["parts"]
        text = " ".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, TypeError):
        return None
    text = _WS_RE.sub(" ", text).strip().strip('"')
    return text or None


def openai_chat_summarize(facts, url, model, key):
    """Summarize via any OpenAI-compatible /chat/completions endpoint (Groq,
    Cerebras, OpenRouter…). Returns the summary, or None on any non-quota failure.
    Raises ProviderQuota on 429."""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user",
                      "content": f"{GEMINI_PROMPT}\n\n{facts}"}],
        "temperature": 0.4,
        "max_tokens": GEMINI_MAX_TOKENS,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key,
                 "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            out = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise ProviderQuota() from e
        return None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    try:
        text = out["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    text = _WS_RE.sub(" ", text or "").strip().strip('"')
    return text or None


def active_providers():
    """Ordered summarizers actually usable right now: (name, call(facts)). Gemini
    first, then each free OpenAI-compatible fallback whose API key is in the env.
    Empty list -> no key anywhere -> pills keep their raw RSS extract."""
    provs = []
    gkey = os.environ.get("GEMINI_API_KEY")
    if gkey:
        provs.append(("gemini", lambda f, k=gkey: gemini_summarize(f, k)))
    for name, env, url, model in OPENAI_COMPAT_PROVIDERS:
        key = os.environ.get(env)
        if key:
            provs.append(
                (name, lambda f, u=url, m=model, k=key: openai_chat_summarize(f, u, m, k)))
    return provs


def summarize_clusters(clusters, verbose=True):
    """Fill cluster['summary'] for DIGEST (news) pills that have an RSS extract,
    rewriting it in new words via the provider chain (Gemini -> Groq -> Cerebras).
    Resilient: a provider's 429 drops only that provider; missing keys / errors ->
    empty summary and the viewer falls back to the extract. Daily-cached per fact."""
    providers = active_providers()
    cache = load_summaries()
    calls = made = cached = fell_back = skipped = 0
    disabled = set()                 # provider names that hit quota this run
    by_provider = {}                 # name -> count, for the log line

    for c in clusters:
        if c.get("kind") != "news":
            continue                                   # only DIGEST is summarized
        # Summarize the HEADLINE item's own extract (the story actually shown), not
        # the longest extract in the cluster — otherwise an imperfectly-grouped
        # sibling can produce a summary that contradicts the headline.
        head_item = next((it for it in c["items"]
                          if it["title"] == c["headline"] and it.get("extract")), None)
        exts = [it["extract"] for it in c["items"] if it.get("extract")]
        if not exts:
            c["summary"], c["summary_src"] = "", "none"   # title-only: nothing to rewrite
            skipped += 1
            continue
        extract = (head_item["extract"] if head_item else max(exts, key=len))
        facts = (f"HEADLINE: {c['headline']}\n\n"
                 f"SOURCE EXTRACT: {extract[:GEMINI_FACTS_MAX]}")
        k = _summary_key(facts)
        if k in cache:
            c["summary"], c["summary_src"] = cache[k], "cache"
            cached += 1
            continue
        live = [(n, call) for n, call in providers if n not in disabled]
        if not live or calls >= GEMINI_MAX_CALLS:
            c["summary"], c["summary_src"] = "", "fallback-extract"
            fell_back += 1
            continue
        # Walk the chain: first provider that returns a summary wins. A 429 disables
        # that provider for the rest of the run and we fall through to the next.
        s, used = "", None
        for name, call in live:
            calls += 1
            try:
                s = call(facts)
            except ProviderQuota:
                disabled.add(name)
                if verbose:
                    print(f"  {name}: rate-limited (429); switching provider…",
                          file=sys.stderr)
                s = ""
                continue
            if s:
                used = name
                break
        if s:
            cache[k] = s
            c["summary"], c["summary_src"] = s, used
            made += 1
            by_provider[used] = by_provider.get(used, 0) + 1
            time.sleep(SUMMARY_SPACING)
        else:
            c["summary"], c["summary_src"] = "", "fallback-extract"
            fell_back += 1

    save_summaries(cache)
    if verbose:
        if not providers:
            why = ("no summarizer key (set GEMINI_API_KEY / GROQ_API_KEY / "
                   "CEREBRAS_API_KEY)")
        else:
            made_by = ", ".join(f"{n}:{by_provider.get(n, 0)}" for n, _ in providers)
            why = f"{made} new ({made_by}), {cached} cached"
        print(f"Summaries: {why}; {fell_back} fell back to RSS extract, "
              f"{skipped} title-only.", file=sys.stderr)
    return {"made": made, "cached": cached, "fell_back": fell_back,
            "skipped": skipped, "had_key": bool(providers)}


# ── refresh / cache ─────────────────────────────────────────────────────────
def now_ts():
    return datetime.now(timezone.utc).timestamp()


def cache_age():
    try:
        with open(STAMP_FILE, encoding="utf-8") as f:
            return now_ts() - float(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def is_stale():
    # A missing/unreadable view cache is always stale, regardless of the stamp:
    # never trust .last_refresh alone, or a stale stamp with no cache leaves the
    # panel stuck on the empty state with no refresh attempt.
    if not os.path.exists(CACHE_FILE):
        return True
    age = cache_age()
    return age is None or age > REFRESH_EVERY


def load_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def fetch_and_build_news(verbose=True):
    """Fetch all feeds, cluster, rank, and Gemini-summarize → a NEWS-ONLY payload
    (no concepts). Needs the Gemini key for summaries (falls back to extracts).
    Returns the payload dict, or None if every source failed. This is the expensive
    part; a maintainer's CI runs it daily so end users never need a key."""
    sources = read_sources()
    if not sources:
        if verbose:
            print("No sources configured in data/feeds.txt", file=sys.stderr)
        return None

    items, ok = [], 0
    for spec in sources:
        try:
            got = fetch_source(spec)
        except SkipSource as e:
            if verbose:
                print(f"  skip {spec} ({e})", file=sys.stderr)
            continue
        except ET.ParseError:
            if verbose:
                print(f"  skip {spec} (bad XML)", file=sys.stderr)
            continue
        except Exception as e:  # noqa: BLE001 — any source error: skip, never crash
            if verbose:
                print(f"  skip {spec} ({e.__class__.__name__})", file=sys.stderr)
            continue
        ok += 1
        # Site-wide feeds (The Verge, MIT Tech Review) have no AI-only edition: keep
        # AI/ML items from THOSE feeds only. Every dedicated AI feed is kept whole.
        # This is scoping, not importance-filtering.
        if any(host in spec for host in GENERAL_FEED_HOSTS):
            kept = [it for it in got
                    if is_ai_relevant(it["title"], it["url"] + " " + it.get("extract", ""))]
            if verbose:
                print(f"  ok   {spec}: {len(kept)}/{len(got)} items (AI-filtered)",
                      file=sys.stderr)
            items.extend(kept)
            continue
        items.extend(got)
        if verbose:
            print(f"  ok   {spec}: {len(got)} items", file=sys.stderr)

    if ok == 0:
        if verbose:
            print("All sources failed.", file=sys.stderr)
        return None

    # Keep it a NEWS digest: drop news older than MAX_AGE_DAYS (undated kept).
    # CHANGELOG/release items are events — keep them regardless of age and let
    # the recency term in the score place stale ones lower.
    cutoff = (datetime.now(timezone.utc).date()
              - timedelta(days=MAX_AGE_DAYS)).isoformat()
    items = [it for it in items
             if it.get("kind") == "changelog" or (not it["date"]) or it["date"] >= cutoff]
    # Drop commerce/promo junk (shopping deals, event ads, newsletter roundups) from
    # every feed — they carry no news value even when "AI" appears in the copy.
    n_pre = len(items)
    items = [it for it in items if not is_promo_junk(it["title"], it.get("url", ""))]
    if verbose and len(items) < n_pre:
        print(f"  dropped {n_pre - len(items)} promo/deal/newsletter items",
              file=sys.stderr)

    # KEEP-ALL: no importance gate. The only removal is deduplication (in
    # build_clusters); weak items are ranked low, never dropped.
    clusters = build_clusters(items)
    # Drop the weak tail: keep only pills with real prose, then cap to the top
    # ranked stories (build_clusters returns them score-sorted). A bare title-only
    # item is never worth a glance, and the long low-score tail is the part the
    # reader dislikes — so it simply never enters the digest.
    n_raw = len(clusters)
    clusters = [c for c in clusters if worth_showing(c)]
    clusters = clusters[:TOP_NEWS]
    # Rewrite DIGEST extracts in new words via Gemini (RELEASE untouched).
    summarize_clusters(clusters, verbose=verbose)
    if verbose:
        multi = sum(1 for c in clusters if c["n_sources"] > 1)
        print(f"Built {len(clusters)} clusters ({multi} multi-source, "
              f"capped from {n_raw}) from {len(items)} items / {ok} sources.",
              file=sys.stderr)
    return {
        "generated_at": int(now_ts()),
        "generated_date": datetime.now(timezone.utc).date().isoformat(),
        "n_items": len(items),
        "n_sources_ok": ok,
        "clusters": clusters,            # NEWS only — concepts are added client-side
    }


PULL_TRIES = 3              # transient-network resilience for the central feed
PULL_BACKOFF = 2.0          # seconds, grows linearly: 2s, 4s, ...


def pull_remote_news(verbose=True):
    """Download the daily-built central NEWS payload (no key needed). None on fail.
    Retries a few times with backoff so a slow link or a transient blip at launch
    doesn't leave the panel stuck on the empty state."""
    last = None
    for attempt in range(1, PULL_TRIES + 1):
        try:
            raw = http_get(REMOTE_URL, timeout=30)
            payload = json.loads(raw.decode("utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("clusters"), list):
                if verbose:
                    print(f"Pulled central digest: {len(payload['clusters'])} clusters "
                          f"(built {payload.get('generated_date', '?')}).",
                          file=sys.stderr)
                return payload
            last = "unexpected payload shape"
        except (SkipSource, ValueError, OSError) as e:
            last = e.__class__.__name__
        if attempt < PULL_TRIES:
            time.sleep(PULL_BACKOFF * attempt)
    if verbose:
        print(f"Could not pull the central feed ({last}) after {PULL_TRIES} tries.",
              file=sys.stderr)
    return None


def build_view_cache(news_payload, verbose=True):
    """Interleave due concepts (per-user spaced repetition) into the news payload and
    write the local view cache the viewer reads. Once per day, so SRS advances daily."""
    clusters = list(news_payload.get("clusters") or [])
    quota = max(1, len(clusters) // CONCEPTS_PER_NEWS)
    concepts = select_due_concepts(quota)
    rotation = interleave_concepts(clusters, concepts)
    payload = dict(news_payload)
    payload["n_concepts_due"] = len(concepts)
    payload["clusters"] = rotation
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    with open(STAMP_FILE, "w", encoding="utf-8") as f:
        f.write(str(int(now_ts())))
    if verbose:
        print(f"View cache: {len(rotation)} pills "
              f"({len(concepts)} concepts interleaved).", file=sys.stderr)
    return True


def refresh(self_refresh=False, verbose=True):
    """Daily client update: get the NEWS payload (pull the central feed by default,
    or build it yourself with --self-refresh), then interleave concepts locally and
    write the view cache. Network-resilient: on failure keep the old cache (False)."""
    news = fetch_and_build_news(verbose) if self_refresh else pull_remote_news(verbose)
    if not news:
        if verbose:
            print("Update failed; keeping the existing cache.", file=sys.stderr)
        return False
    return build_view_cache(news, verbose)


def build_news_to_file(path, verbose=True):
    """Maintainer/CI: build the central NEWS payload and write it to `path`."""
    news = fetch_and_build_news(verbose)
    if not news:
        return False
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(news, f, ensure_ascii=False, indent=1)
    if verbose:
        print(f"Wrote central news cache → {path}", file=sys.stderr)
    return True


# ── rendering ───────────────────────────────────────────────────────────────
def open_url(url):
    if not url:
        return False
    try:
        if webbrowser.open(url):
            return True
    except Exception:  # noqa: BLE001
        pass
    for opener in ("open", "xdg-open"):
        if shutil.which(opener):
            try:
                subprocess.Popen([opener, url],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                return True
            except Exception:  # noqa: BLE001
                pass
    return False


def pill_lines(cluster, inner):
    """Content lines (style, plain-text) for one cluster — dark-theme structure:
    a big title + thin rule, the short summary, then per-source sub-paragraphs
    (▸ sub-title · the longer extract · the source in detail)."""
    kind = cluster.get("kind", "news")
    lines = []
    head = _WS_RE.sub(" ", cluster.get("headline", "")).strip()

    # BIG TITLE, full and exhaustive, with a thin underline rule.
    for w in textwrap.wrap(head, width=inner) or [""]:
        lines.append((D_TITLE, w))
    lines.append((D_RULE, "─" * min(inner, max(8, len(head)))))

    if kind == "concept":
        lines.append((D_HINT, "evergreen concept · spaced repetition"))
        return lines

    items = cluster.get("items") or []

    # SHORT SUMMARY (our Gemini rewrite).
    summary = cluster.get("summary") if kind == "news" else ""
    if summary:
        lines.append(("", ""))
        for w in textwrap.wrap(summary, width=inner):
            lines.append((D_SUMMARY, w))
        # Provenance: label the rewrite as machine-generated (never passed off as
        # human journalism) — the linked original below stays canonical.
        lines.append((D_HINT, "↻ AI summary · may be imperfect"))

    # PER-SOURCE ATTRIBUTION. The summary above already IS the rewritten extract, so
    # repeating the raw extract here just says the same thing, shorter (and truncated).
    # Instead show each outlet's OWN headline — which differs across outlets and adds
    # perspective for multi-source stories — then source · date · link. For the outlet
    # whose headline matches the card title, go straight to the source line.
    for it in items:
        lines.append(("", ""))
        title = _WS_RE.sub(" ", (it.get("title") or "")).strip()
        if title and title.lower() != head.lower():
            for w in textwrap.wrap(f"▸ {title}", width=inner) or [""]:
                lines.append((D_SUB, w))
            indent = "  "
        else:
            indent = ""
        if it.get("note"):
            lines.append((D_META, indent + it["note"]))
        date = f" · {it['date']}" if it.get("date") else ""
        lines.append((D_META, f"{indent}— {it.get('source', '')}{date}"))
        if it.get("url"):
            lines.append((D_LINK, f"{indent}↗ {_domain(it['url'])}"))
    return lines


# pane header tag by kind — the first word tells you what you're looking at.
TAGS = {"news": "DIGEST", "changelog": "RELEASE", "concept": "LEARN"}


def _pretty_date(iso):
    try:
        dt = datetime.fromisoformat((iso or "")[:10])
        return dt.strftime("%b ") + str(dt.day)
    except (ValueError, TypeError):
        return ""


def _pane_size():
    size = shutil.get_terminal_size((80, 24))
    return max(30, min(size.columns, 120)), max(8, size.lines)


def render(cluster, idx, total, interval, offset, flash=""):
    cols, rows = _pane_size()
    inner = cols - 2                       # 1 leading margin; lines pad to full width
    content = pill_lines(cluster, inner)

    top_pad = max(1, min(4, rows // 8))    # breathing room so the title isn't at row 0
    view_h = max(3, rows - 2 - top_pad)    # top pad + header(1) + content + footer(1)
    max_off = max(0, len(content) - view_h)
    offset = max(0, min(offset, max_off))
    window = content[offset:offset + view_h]

    def blk(style, text):
        """A full-width line painted on the black pane background."""
        t = text[:inner]
        pad = max(0, cols - 1 - len(t))
        return f"{PANE_BG} {style}{t}{' ' * pad}{RESET}"

    # Top breathing room, then header:  TAG                       3/150 · Jun 23
    tag = TAGS.get(cluster.get("kind", "news"), "DIGEST")
    right = f"{idx + 1}/{total}"
    pretty = _pretty_date(cluster.get("latest"))
    if pretty:
        right += f" · {pretty}"
    gap = max(1, cols - 1 - len(tag) - len(right))
    out = [blk("", "") for _ in range(top_pad)]
    out.append(f"{PANE_BG} {D_TAG}{tag}{RESET}{PANE_BG}{' ' * gap}{D_HINT}{right}{RESET}")

    for style, text in window:
        out.append(blk(style, text))
    out.extend(blk("", "") for _ in range(view_h - len(window)))

    if flash:
        out.append(blk(D_SUB, flash))
    else:
        scroll = f"   ↑/↓ {offset}/{max_off}" if max_off > 0 else ""
        out.append(blk(D_HINT, f"n/→ next · p/← prev · o open · s save · "
                               f"q quit · {int(interval)}s{scroll}"))
    return "\n".join(out), max_off


def draw(cluster, idx, total, interval, offset, flash=""):
    frame, max_off = render(cluster, idx, total, interval, offset, flash)
    sys.stdout.write(CLEAR + frame)        # no trailing newline → full pane painted
    sys.stdout.flush()
    return max_off


# ── non-blocking input ──────────────────────────────────────────────────────
def read_key(timeout):
    r, _, _ = select.select([sys.stdin], [], [], timeout)
    if not r:
        return None
    ch = sys.stdin.read(1)
    if not ch:
        return "quit"
    if ch == "\x1b":
        r2, _, _ = select.select([sys.stdin], [], [], 0.02)
        seq = sys.stdin.read(2) if r2 else ""
        return {"[C": "next", "OC": "next", "[D": "prev", "OD": "prev",
                "[A": "up", "OA": "up", "[B": "down", "OB": "down"}.get(seq)
    return {
        "n": "next", " ": "next", "\t": "next",
        "p": "prev",
        "o": "open",
        "s": "save",
        "k": "up", "j": "down",
        "q": "quit", "\x03": "quit",
    }.get(ch)


def save_pill(cluster):
    """Append the current pill (title + source + link + date) to saved.txt.
    Plain text, hand-readable, personal (gitignored). Returns a short status."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    lines = [f"## {cluster['headline']}"]
    if cluster.get("kind") == "concept":
        lines.append(f"   {cluster.get('category', 'CONCEPT')} · evergreen concept")
    else:
        for it in cluster.get("items", []):
            date = f" · {it['date']}" if it.get("date") else ""
            lines.append(f"   {it['source']}{date}")
            if it.get("url"):
                lines.append(f"   {it['url']}")
    lines.append(f"   saved {stamp}")
    try:
        with open(SAVED_FILE, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n\n")
        return "saved ✓  → data/saved.txt"
    except OSError as e:
        return f"save failed ({e.__class__.__name__})"


def dwell_for(n_lines, base):
    """Per-pill dwell seconds, scaling with length: a short pill sits ~DWELL_MIN,
    a long multi-source one rises toward the ceiling. `base` (--interval) sets
    that ceiling, so the user's interval stays the global knob."""
    hi = max(DWELL_MIN, base * 1.5)
    frac = min(1.0, n_lines / 28.0)
    return DWELL_MIN + (hi - DWELL_MIN) * frac


def load_pos(total):
    """Last shown pill index, clamped into range. Missing/garbage -> 0."""
    try:
        with open(POS_FILE, encoding="utf-8") as f:
            p = int(f.read().strip())
        return p % total if total else 0
    except (OSError, ValueError):
        return 0


def save_pos(pos):
    """Persist the current pill index so a restart resumes here."""
    try:
        os.makedirs(CC_STATE_DIR, exist_ok=True)
        with open(POS_FILE, "w", encoding="utf-8") as f:
            f.write(str(pos))
    except OSError:
        pass


def read_cc_state():
    """Return "WAITING" or "WORKING". Missing/unreadable file -> WORKING (fail-safe:
    better to show the pills than to sit on a black panel)."""
    try:
        with open(CC_STATE_FILE, encoding="utf-8") as f:
            return "WAITING" if f.read().strip().upper() == "WAITING" else "WORKING"
    except OSError:
        return "WORKING"


def cc_state_poller():
    """A poll() that stats the state file and re-reads it only when mtime changes —
    so the per-tick check is a cheap stat, not a read. Fail-safe to WORKING."""
    cache = {"mtime": None, "state": "WORKING"}

    def poll():
        try:
            m = os.stat(CC_STATE_FILE).st_mtime
        except OSError:
            cache["mtime"] = None
            return "WORKING"
        if m != cache["mtime"]:
            cache["mtime"] = m
            cache["state"] = read_cc_state()
        return cache["state"]

    return poll


def draw_waiting():
    """Hide the digest: paint the pane black with one neutral marker at the top."""
    cols, rows = _pane_size()
    blank = f"{PANE_BG}{' ' * cols}{RESET}"
    top = f"{PANE_BG} {D_HINT}⏸{' ' * max(0, cols - 2)}{RESET}"
    sys.stdout.write(CLEAR + "\n".join([top] + [blank] * (rows - 1)))
    sys.stdout.flush()


# ── main loop ───────────────────────────────────────────────────────────────
def run(clusters, interval):
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    sys.stdout.write(HIDE_CUR)
    poll_state = cc_state_poller()

    # Self-heal: while the empty state is showing, keep retrying the central pull
    # in the background and swap real news in the instant it lands — no restart,
    # no user action. A single failed pull at launch must not kill the session.
    heal = {"on": bool(clusters and clusters[0].get("_empty")),
            "busy": False, "ok": False, "next": time.monotonic() + 5}

    def _bg_pull():
        try:
            done = refresh(verbose=False)
        except Exception:        # noqa: BLE001 — never crash the viewer
            done = False
        heal["ok"] = bool(done)
        heal["busy"] = False

    try:
        tty.setcbreak(fd)
        total = len(clusters)
        pos, offset, flash = load_pos(total), 0, ""   # resume where we left off
        state = poll_state()
        while True:
            # ── draw the current view (only redrawn on a real change, not per poll) ──
            if state == "WAITING":
                draw_waiting()
                max_off, hold, was_flashing = 0, 3600.0, False   # hidden: no auto-rotate
            else:
                cols = max(34, min(shutil.get_terminal_size((80, 24)).columns, 100))
                dwell = dwell_for(len(pill_lines(clusters[pos], cols - 2)), interval)
                hold = 1.2 if flash else dwell      # show a "saved ✓" flash briefly
                was_flashing = bool(flash)
                max_off = draw(clusters[pos], pos, total, dwell, offset, flash)
                save_pos(pos)                       # remember it for the next restart
                flash = ""

            # ── wait: poll the state file in small chunks; break on key / state-change ──
            deadline = time.monotonic() + hold
            action = None
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    action = "tick"
                    break
                key = read_key(min(remaining, CC_POLL_SECS))
                new_state = poll_state()
                if new_state != state:
                    state = new_state               # WORKING<->WAITING transition
                    action = "state"
                    break
                if heal["on"]:                      # empty state: keep trying to pull
                    if heal["ok"]:
                        action = "reload"
                        break
                    if not heal["busy"] and time.monotonic() >= heal["next"]:
                        heal["busy"] = True
                        heal["next"] = time.monotonic() + 60
                        threading.Thread(target=_bg_pull, daemon=True).start()
                if key:
                    action = key
                    break

            # ── handle ──
            if action == "quit":
                break
            if action == "reload":                  # background pull landed: swap in
                new = load_cache()
                nc = [c for c in (new or {}).get("clusters", []) if worth_showing(c)]
                if nc:
                    clusters, total = nc, len(nc)
                    pos, offset, flash = 0, 0, ""
                    heal["on"] = False
                else:
                    heal["ok"] = False              # nothing usable yet; keep retrying
                continue
            if action == "state":
                continue                            # redraw at top: ⏸ or resume SAME pill
            if state == "WAITING":
                continue                            # hidden: swallow keys (q already handled)
            if action == "next":
                pos = (pos + 1) % total
                offset = 0
            elif action == "prev":
                pos = (pos - 1) % total
                offset = 0
            elif action == "down":
                offset = min(offset + 3, max_off)
            elif action == "up":
                offset = max(offset - 3, 0)
            elif action == "open":
                items = clusters[pos].get("items") or []
                if items:
                    open_url(items[0]["url"])
            elif action == "save":
                flash = save_pill(clusters[pos])
            elif action == "tick" and not was_flashing:
                pos = (pos + 1) % total             # auto-advance (not after a flash)
                offset = 0
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write(SHOW_CUR + RESET + "\n")
        sys.stdout.flush()


def empty_state_cluster(msg):
    return {
        "headline": "No news yet",
        "latest": "",
        "n_sources": 0,
        "kind": "news",
        "_empty": True,                 # viewer watches this to keep retrying the pull
        "items": [{"title": "", "source": "Meanwhile", "date": "",
                   "extract": msg, "url": "", "kind": "news"}],
        "score": 0,
    }


def recap(days=7, top=8):
    """Print the week's highest-signal items (releases, big clusters, launches)
    by priority score, then exit. Non-interactive — concepts are excluded; this
    is the moment the stream of fragments becomes a synthesis you remember."""
    cache = load_cache()
    clusters = (cache or {}).get("clusters") or []
    if not clusters:
        print("No cache yet — run ./news --refresh first.")
        return 0
    today = datetime.now(timezone.utc).date()

    def within(c):
        if c.get("kind") == "concept":
            return False                       # recap = updates, not evergreen
        try:
            return (today - datetime.fromisoformat(c["latest"]).date()).days <= days
        except (ValueError, TypeError):
            return c.get("kind") == "changelog"   # undated release: keep

    picks = sorted((c for c in clusters if within(c)),
                   key=lambda c: c.get("score", 0), reverse=True)[:top]
    gen = (cache or {}).get("generated_date", "?")
    print(f"Meanwhile · recap · top {len(picks)} of the last {days} days "
          f"(cache {gen})")
    print("─" * 64)
    if not picks:
        print("Nothing in the window yet.")
    for i, c in enumerate(picks, 1):
        tag = TAGS.get(c.get("kind", "news"), "DIGEST")
        where = "release" if c.get("kind") == "changelog" else f"{c['n_sources']} source(s)"
        print(f"\n{i:>2}. [{tag}] {c['headline']}")
        print(f"     {where} · {c.get('latest') or 'n/a'} · score {c.get('score', 0):.2f}")
        items = c.get("items") or []
        if items and items[0].get("url"):
            print(f"     {items[0]['url']}")
    print()
    return 0


def list_all():
    """Print EVERY news card in the current cache (rank order) with its summary,
    sources and link — a one-screen report for reviewing the whole digest. Reads
    the cache only; pair with --refresh-only first to review the freshest feed."""
    cache = load_cache()
    clusters = (cache or {}).get("clusters") or []
    if not clusters:
        print("No cache yet — run ./news --refresh first.")
        return 0
    news = [c for c in clusters if c.get("kind") == "news"]
    gen = (cache or {}).get("generated_date", "?")
    width = max(48, min(100, shutil.get_terminal_size((100, 24)).columns - 2))
    print(f"Meanwhile · all {len(news)} cards (cache {gen})")
    print("─" * width)
    for i, c in enumerate(news, 1):
        items = c.get("items") or []
        srcs = ", ".join(sorted({it.get("source", "?") for it in items}))
        body = _WS_RE.sub(" ", (c.get("summary")
                          or (items[0].get("extract", "") if items else ""))).strip()
        src_tag = c.get("summary_src", "")
        mark = "↻" if c.get("summary") else "·"   # ↻ = LLM summary, · = raw extract
        head = textwrap.fill(f"#{i:>2} [{c.get('n_sources', 1)} src] {c['headline']}",
                             width=width, subsequent_indent="       ")
        print(f"\n{head}")
        print(f"     {srcs} · {c.get('latest') or 'n/a'}"
              + (f" · {src_tag}" if src_tag else ""))
        if body:
            # word-wrapped with a hanging indent — never breaks mid-word
            print(textwrap.fill(body, width=width,
                                initial_indent=f"     {mark} ",
                                subsequent_indent="       "))
        if items and items[0].get("url"):
            print(f"     ↗ {items[0]['url']}")
    print()
    return 0


SEP = " · "
_VER_RE = re.compile(r"v?\d+\.\d+(?:\.\d+)*(?:[-+.][0-9A-Za-z][0-9A-Za-z.]*)*")
# Release-note boilerplate that carries no information on a single line.
_BOILER_RE = re.compile(
    r"\b(what'?s (?:changed|new)|notable changes|misc changes|full changelog|"
    r"change ?log|release notes|highlights)\b[:\-—]*", re.I)
_LABEL_LEAD_RE = re.compile(
    r"^\s*(features?|changes?|fixes?|improvements?|bug ?fixes?)\b[:\-—\s]*", re.I)
_TLDS = (".com", ".org", ".io", ".ai", ".net", ".dev", ".co")


def _wtrunc(s, n):
    """Truncate to <= n chars on a WORD boundary (never mid-word), with an ellipsis."""
    s = s.strip()
    if n <= 1:
        return "…" if s else ""
    if len(s) <= n:
        return s
    cut = s[:n - 1]
    sp = cut.rfind(" ")
    if sp >= int(n * 0.6):            # break at the space unless it's far too early
        cut = cut[:sp]
    return cut.rstrip(" ,;:·—-") + "…"


def _host_brand(url):
    host = urllib.parse.urlparse(url or "").netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    for t in _TLDS:
        if host.endswith(t):
            return host[: -len(t)]
    return host


def _essential_title(pill):
    """Trim a release title to 'repo version' (drop dates, '(Current)', author).
    Non-releases keep their headline."""
    head = _WS_RE.sub(" ", pill.get("headline", "")).strip()
    if pill.get("kind") != "changelog":
        return head
    items = pill.get("items") or []
    repo = (items[0].get("source") if items else "") or head.split(" ")[0]
    m = _VER_RE.search(head)
    if m:
        ver = m.group(0)
    else:                            # tags like REL_17_2: strip noise, keep the rest
        ver = head[len(repo):] if head.lower().startswith(repo.lower()) else head
        ver = re.sub(r"\(.*?\)", "", ver)
        ver = re.sub(r"@\S+", "", ver)
        ver = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "", ver)
        ver = re.sub(r"\bversion\b", "", ver, flags=re.I)
        ver = _WS_RE.sub(" ", ver).strip(" ,-—·")
    return _WS_RE.sub(" ", f"{repo} {ver}").strip()


def _extract_gist(pill, title):
    """The part of the extract that TEACHES — boilerplate labels and a duplicated
    version stripped. Prefers our Gemini summary (new words) for news. Empty if
    there's nothing informative to show."""
    items = pill.get("items") or []
    raw = pill.get("summary") or (items[0].get("extract") if items else "") or ""
    e = _WS_RE.sub(" ", raw).strip()
    if not e:
        return ""
    e = _BOILER_RE.sub(" ", e)
    e = _LABEL_LEAD_RE.sub("", e)
    e = _WS_RE.sub(" ", e).strip(" :·—-✨📝🐛🚀🔧✅⚡🎉#")
    m = _VER_RE.search(title or "")              # rule 3: don't repeat the version
    if m and m.group(0) in e:
        e = _WS_RE.sub(" ", e.replace(m.group(0), "")).strip(" ·-")
    return e if len(e) >= 12 else ""


def status_format(pill, width):
    """One dense, ANSI-colored line that uses the full width:
    '▸ TAG  essential title · teaching bit of the extract · short source'.
    Truncates only at the end, only on word boundaries. Priority: title > gist >
    source; the source is dropped first when space runs out."""
    kind = pill.get("kind", "news")
    tag = TAGS.get(kind, "DIGEST")
    color = {"news": HEAD, "changelog": SRC, "concept": LINK}.get(kind, HEAD)
    base = f"▸ {tag}  "
    budget = max(8, width - len(base))

    if kind == "concept":
        # The dense sentence is already optimal — just never cut it mid-word.
        body = _wtrunc(_WS_RE.sub(" ", pill.get("headline", "")).strip(), budget)
        return f"{DIM}▸ {RESET}{color}{BOLD}{tag}{RESET}  {body}{RESET}"

    items = pill.get("items") or []
    url = items[0].get("url", "") if items else ""
    if kind == "changelog":
        src = _host_brand(url) or "release"
    else:
        name = (items[0].get("source") if items else "") or ""
        src = name if 0 < len(name) <= 22 else (_host_brand(url) or name[:22])

    title = _wtrunc(_essential_title(pill), budget)
    remaining = budget - len(title)

    src_seg = ""
    if src and len(SEP) + len(src) <= remaining:
        src_seg = SEP + src
        remaining -= len(src_seg)

    gist_seg = ""
    gist = _extract_gist(pill, title)
    if gist and remaining >= len(SEP) + 6:
        gist_seg = SEP + _wtrunc(gist, remaining - len(SEP))

    return (f"{DIM}▸ {RESET}{color}{BOLD}{tag}{RESET}  "
            f"{BOLD}{title}{RESET}{DIM}{gist_seg}{src_seg}{RESET}")


def status_width(stdin_json):
    """The REAL terminal width to fill, in priority order:
    1. an explicit width in Claude Code's stdin JSON, IF a future version adds one
       (today it does not — its keys are session_id, model, cost, context_window,
       …, with no width/columns/terminal field);
    2. the real terminal — Claude Code sets COLUMNS when it runs the command, and
       shutil.get_terminal_size() reads it (this is what actually works today);
    3. a HIGH fallback (never the cramped 80) when the width is truly unknown.
    Capped only by a generous sanity ceiling so a wide terminal is never clipped."""
    for path in (("terminal", "width"), ("terminal", "columns"),
                 ("width",), ("columns",)):
        node = stdin_json
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, int) and node > 0:
            return min(node, STATUS_MAXW)
    cols = shutil.get_terminal_size((0, 0)).columns
    if cols and cols >= 24:
        return min(cols, STATUS_MAXW)
    return STATUS_FALLBACK_W


def statusline():
    """Fast, non-interactive, NETWORK-FREE one-liner for Claude Code's statusLine.
    Reads only the ready cache; on no/empty cache prints a neutral line. Always 0."""
    # Claude Code pipes its context JSON on stdin. Read it (best-effort): we don't
    # need most of it, but a future CC may add the terminal width there.
    raw_stdin = ""
    try:
        if not sys.stdin.isatty():
            raw_stdin = sys.stdin.read()
    except Exception:  # noqa: BLE001 — stdin is best-effort only
        pass
    stdin_json = {}
    if raw_stdin.strip():
        try:
            stdin_json = json.loads(raw_stdin)
        except ValueError:
            stdin_json = {}

    clusters = (load_cache() or {}).get("clusters") or []
    width = status_width(stdin_json)
    if not clusters:
        sys.stdout.write(f"{DIM}▸ Meanwhile · no cache yet — run "
                         f"./news --refresh{RESET}")
        return 0
    pool = clusters[:STATUS_POOL]
    # TIME-based index: stable within STATUS_WINDOW, deterministic, no flicker.
    idx = int(now_ts() // STATUS_WINDOW) % len(pool)
    sys.stdout.write(status_format(pool[idx], width))
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Daily clustered AI/tech news digest for a side terminal pane.")
    ap.add_argument("--interval", type=float, default=40.0,
                    help="seconds between auto-rotations (default: 40)")
    ap.add_argument("--refresh", action="store_true",
                    help="force a refresh now, then run")
    ap.add_argument("--refresh-only", action="store_true",
                    help="refresh the cache and exit (no viewer)")
    ap.add_argument("--recap", action="store_true",
                    help="print the week's top items by priority and exit")
    ap.add_argument("--list", dest="list_all", action="store_true",
                    help="print EVERY card (summary + sources + link) and exit")
    ap.add_argument("--statusline", action="store_true",
                    help="print ONE cache-only line for Claude Code's statusLine "
                         "and exit (never touches the network)")
    ap.add_argument("--self-refresh", dest="self_refresh", action="store_true",
                    help="build the digest yourself from the feeds (needs your own "
                         "GEMINI_API_KEY) instead of pulling the central feed")
    ap.add_argument("--build-news", dest="build_news", action="store_true",
                    help="maintainer/CI: build the central NEWS cache and exit")
    ap.add_argument("--out", default=DIST_FILE,
                    help=f"output path for --build-news (default: {DIST_FILE})")
    args = ap.parse_args()
    if args.interval <= 0:
        ap.error("--interval must be positive")

    # Statusline is the fast path: cache-only, no network, never blocks CC.
    if args.statusline:
        return statusline()

    # Maintainer/CI: build the central, pre-summarized news cache and exit.
    if args.build_news:
        print("Building central news cache...", file=sys.stderr)
        return 0 if build_news_to_file(args.out, verbose=True) else 1

    if args.refresh or args.refresh_only or (
            is_stale() and not args.recap and not args.list_all):
        how = "building locally" if args.self_refresh else "pulling central feed"
        if is_stale() and not (args.refresh or args.refresh_only):
            print(f"Cache is stale (>24h); updating once ({how})...", file=sys.stderr)
        else:
            print(f"Updating ({how})...", file=sys.stderr)
        try:
            refresh(self_refresh=args.self_refresh, verbose=True)
        except Exception as e:  # noqa: BLE001 — last-resort guard
            print(f"Update failed ({e.__class__.__name__}); using cache.",
                  file=sys.stderr)

    if args.refresh_only:
        return 0
    if args.recap:
        return recap()
    if args.list_all:
        return list_all()

    cache = load_cache()
    clusters = (cache or {}).get("clusters") or []
    # Drop thin, low-value news pills (no summary AND no real extract); keep the
    # concepts and releases. A pill with nothing to say isn't worth a glance.
    clusters = [c for c in clusters if worth_showing(c)]
    if not clusters:
        clusters = [empty_state_cluster(
            "Fetching today's digest… this auto-retries every minute, so it will "
            "appear as soon as the network lets it through. (You can force it with "
            "./news --refresh.)")]

    if not sys.stdin.isatty():
        c = clusters[0]
        print(f"DIGEST · {c['headline']}  [{c['n_sources']} source(s)]")
        for it in c["items"]:
            print(f"  ▸ {it['source']} · {it['date']}: {it['url']}")
        return 0

    try:
        run(clusters, args.interval)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
