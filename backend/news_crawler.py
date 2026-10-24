from __future__ import annotations

import hashlib
import math
import random
import re
import string
import time
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from threading import Lock
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from requests.exceptions import HTTPError, RequestException
from urllib3.util.retry import Retry

import news_storage
from news_config import MAX_WORKERS, REGION_ALIASES, SIMILARITY_THRESHOLD, SOURCES, TIMEOUT, USER_AGENTS

domain_locks: dict[str, Lock] = {}
last_request_time: dict[str, float] = {}
state_lock = Lock()
storage_lock = Lock()


def _first_node(parent, *tags):
    for tag in tags:
        node = parent.find(tag)
        if node is not None:
            return node
    return None


def get_domain_lock(url: str) -> tuple[Lock, str]:
    domain = urlparse(url).netloc
    with state_lock:
        if domain not in domain_locks:
            domain_locks[domain] = Lock()
            last_request_time[domain] = 0
        return domain_locks[domain], domain


def fetch_url_with_rate_limit(url: str) -> tuple[bytes, str | None]:
    lock, domain = get_domain_lock(url)
    with lock:
        elapsed = time.time() - last_request_time[domain]
        delay = random.uniform(1.0, 2.0)
        if elapsed < delay:
            time.sleep(delay - elapsed)

        session = requests.Session()
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
        session.mount("http://", HTTPAdapter(max_retries=retry))
        session.mount("https://", HTTPAdapter(max_retries=retry))

        headers = {"User-Agent": random.choice(USER_AGENTS)}
        response = session.get(url, headers=headers, timeout=TIMEOUT)
        response.raise_for_status()

        if response.encoding == "ISO-8859-1":
            response.encoding = response.apparent_encoding

        last_request_time[domain] = time.time()
        return response.content, response.encoding


def clean_mojibake(text: str) -> str:
    if not text:
        return ""
    try:

        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):

        return text


def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = clean_mojibake(text)
    text = re.sub(r"<[^>]+>", "", text)
    text = unicodedata.normalize("NFKC", text)
    text = " ".join(text.split())
    return text.strip()


def get_similarity(text_a: str, text_b: str) -> float:
    def get_tokens(text: str) -> set[str]:
        if not text:
            return set()
        table = str.maketrans("", "", string.punctuation)
        return set(text.lower().translate(table).split())

    set_a = get_tokens(text_a)
    set_b = get_tokens(text_b)
    if not set_a or not set_b:
        return 0
    return len(set_a & set_b) / len(set_a | set_b)


def generate_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_valid_headline(headline: str, link: str, source_url: str) -> bool:
    if not headline or len(headline) < 15:
        return False
    if link.rstrip("/") == source_url.rstrip("/"):
        return False

    noise = [
        "read more",
        "click here",
        "subscribe",
        "latest updates",
        "home",
        "privacy policy",
        "contact us",
        "about us",
        "follow us",
        "breaking news",
        "live updates",
    ]
    lowered = headline.lower()
    if any(entry in lowered for entry in noise):
        return False
    if any(lowered.startswith(label + ":") for label in ("video", "audio", "gallery")):
        return False
    if len(headline.split()) < 3:
        return False
    return True


SUMMARY_CHARS = 280


def parse_time(text: str | None) -> datetime | None:
    """A feed's date (ISO 8601 or RFC 822, e.g. "Sat, 24 Oct 2026 12:43:38 +0530"), or None."""
    if not text:
        return None
    text = text.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def summarize(text: str | None) -> str:
    """A feed's description as one short plain-text paragraph."""
    summary = normalize_text(text or "")
    if len(summary) <= SUMMARY_CHARS:
        return summary
    return summary[:SUMMARY_CHARS].rsplit(" ", 1)[0] + "…"


def parse_rss(content: bytes, source_config: dict) -> list[dict]:
    results = []
    max_items = source_config.get("max_items", 10)
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return results

    items = root.findall(".//item") or root.findall(".//{*}entry")
    for item in items[:max_items]:
        title_node = _first_node(item, "title", "{*}title")
        link_node = _first_node(item, "link", "{*}link")
        pub_node = _first_node(item, "pubDate", "{*}pubDate", "published", "{*}published", "updated", "{*}updated")
        summary_node = _first_node(item, "description", "{*}summary", "{*}description")

        headline = normalize_text(title_node.text if title_node is not None and title_node.text else "")
        link = ""
        if link_node is not None:
            link = link_node.text if link_node.text else link_node.attrib.get("href", "")
        published = parse_time(pub_node.text if pub_node is not None else None) or datetime.now(timezone.utc)

        if is_valid_headline(headline, link, source_config["url"]):
            results.append(
                {
                    "headline": headline,
                    "link": link.strip(),
                    "summary": summarize(summary_node.text if summary_node is not None else None),
                    "source": source_config["name"],
                    "language": source_config["language"],
                    "category": source_config.get("category", "india"),
                    "timestamp": published.isoformat(),
                }
            )
    return results


def parse_html(content: bytes, source_config: dict, encoding: str | None) -> tuple[list[dict], bool]:
    soup = BeautifulSoup(content, "lxml", from_encoding=encoding)
    selectors = [selector.strip() for selector in source_config["selector"].split(",")]
    elements = []
    used_fallback = False
    max_items = source_config.get("max_items", 10)

    for selector in selectors:
        found = soup.select(selector)
        if found:
            for element in found:
                if element not in elements:
                    elements.append(element)
            if len(elements) >= max_items:
                break

    if not elements:
        used_fallback = True
        elements = soup.find_all(["h1", "h2", "h3"])

    results = []
    seen_headlines = set()
    for element in elements[:max_items]:
        headline = normalize_text(element.get_text())
        if headline in seen_headlines:
            continue

        link_tag = element if element.name == "a" else element.find("a")
        if not link_tag:
            link_tag = element.find_parent("a")
        if not link_tag:
            container = element.find_parent(["div", "li", "section", "article"])
            if container:
                link_tag = container.find("a")

        link = urljoin(source_config["url"], link_tag["href"]) if link_tag and link_tag.has_attr("href") else source_config["url"]
        if is_valid_headline(headline, link, source_config["url"]):
            results.append(
                {
                    "headline": headline,
                    "link": link,
                    "summary": "",
                    "source": source_config["name"],
                    "language": source_config["language"],
                    "category": source_config.get("category", "india"),
                    # A web page lists no dates: the time it was first seen stands in.
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            seen_headlines.add(headline)

    return results, used_fallback


def list_news_sources() -> list[dict]:
    return [
        {"name": source["name"], "language": source["language"], "type": source["type"], "category": source["category"]}
        for source in SOURCES
    ]


def _resolve_sources(source_query: str | None) -> list[dict]:
    if not source_query:
        return SOURCES
    return [source for source in SOURCES if source_query.lower() in source["name"].lower()]


def fetch_source(source: dict, limit: int | None = None) -> tuple[list[dict], dict]:
    """Fetch and parse one source. stats also carry the source's category and how
    long it took (rate-limit waits included), for the crawl health table."""
    started = time.perf_counter()
    items, stats = _fetch_source(source, limit)
    stats["category"] = source.get("category")
    stats["duration_ms"] = round((time.perf_counter() - started) * 1000)
    if stats["status"] == "Success" and not items:
        stats["status"] = "No items"  # page loaded but nothing parsed: its layout probably changed
    return items, stats


def _fetch_source(source: dict, limit: int | None = None) -> tuple[list[dict], dict]:
    stats = {
        "name": source["name"],
        "extracted": 0,
        "status": "Success",
        "fallback": False,
    }
    source_config = source.copy()
    if limit:
        source_config["max_items"] = limit

    try:
        content, encoding = fetch_url_with_rate_limit(source["url"])
        if not content:
            stats["status"] = "Fetch Failed"
            return [], stats

        if source["type"] == "rss":
            items = parse_rss(content, source_config)
        else:
            items, used_fallback = parse_html(content, source_config, encoding)
            stats["fallback"] = used_fallback

        stats["extracted"] = len(items)
        return items, stats
    except HTTPError as error:
        stats["status"] = f"HTTP {error.response.status_code}"
    except RequestException:
        stats["status"] = "Network Error"
    except Exception as error:
        stats["status"] = f"Error: {type(error).__name__}"
    return [], stats


def crawl_news(source_query: str | None = None, dry_run: bool = False, limit: int | None = None) -> dict:
    active_sources = _resolve_sources(source_query)
    if source_query and not active_sources:
        return {
            "status": "no_match",
            "message": f"No source matching '{source_query}' was found.",
            "saved": 0,
            "headlines": [],
            "summary": [],
        }

    all_raw = []
    summary_stats = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_source, source, limit): source for source in active_sources}
        for future in as_completed(futures):
            items, stats = future.result()
            all_raw.extend(items)
            summary_stats.append(stats)

    # The background refresher and an on-demand crawl can finish at the same
    # time; serialize read-dedupe-write so neither overwrites the other's hashes.
    with storage_lock:
        seen_hashes = news_storage.load_seen_hashes()
        unique = []
        duplicate_count = 0
        for record in all_raw:
            record_hash = generate_hash(record["headline"])
            if record_hash in seen_hashes:
                duplicate_count += 1
                continue
            if any(
                record["language"] == existing["language"]
                and get_similarity(record["headline"], existing["headline"]) > SIMILARITY_THRESHOLD
                for existing in unique
            ):
                duplicate_count += 1
                continue
            record["hash"] = record_hash
            unique.append(record)
            if not dry_run:
                seen_hashes.add(record_hash)

        saved_by_source: dict[str, int] = {}
        for record in unique:
            saved_by_source[record["source"]] = saved_by_source.get(record["source"], 0) + 1
        for stats in summary_stats:
            stats["saved"] = saved_by_source.get(stats["name"], 0)

        output_file = None
        if not dry_run and unique:
            output_file = str(news_storage.append_to_jsonl(unique))
            news_storage.save_seen_hashes(seen_hashes)

    return {
        "status": "ok",
        "source_query": source_query,
        "dry_run": dry_run,
        "total_extracted": len(all_raw),
        "duplicates": duplicate_count,
        "saved": len(unique),
        "output_file": output_file,
        "headlines": unique,
        "summary": sorted(summary_stats, key=lambda item: item["name"]),
    }


def get_latest_headlines(limit: int = 10, source: str | None = None, language: str | None = None) -> dict:
    records = news_storage.read_jsonl(news_storage.get_latest_file())
    if source:
        records = [record for record in records if source.lower() in record["source"].lower()]
    if language:
        records = [record for record in records if record["language"] == language]
    records = list(reversed(records))[:limit]
    return {
        "status": "ok",
        "count": len(records),
        "headlines": records,
    }


# --------------------------------------------------------------------------- search

# Headlines saved before categories existed get theirs from their source.
_SOURCE_CATEGORY = {source["name"]: source["category"] for source in SOURCES}
_TOPIC_NOISE = {
    "news", "latest", "today", "todays", "headlines", "headline", "top", "update", "updates",
    "the", "and", "about", "from", "for", "in", "of", "on",
}
MAX_AGE_HOURS = 72


def category_of(record: dict) -> str:
    return record.get("category") or _SOURCE_CATEGORY.get(record.get("source", ""), "india")


def _topic_words(topic: str) -> list[str]:
    words = [w for w in re.findall(r"\w+", topic.lower()) if w not in _TOPIC_NOISE]
    # "elections" should find "election": match on the singular as a word prefix.
    return [w[:-1] if len(w) > 3 and w.isascii() and w.endswith("s") else w for w in words]


def matches_topic(record: dict, topic: str) -> bool:
    """Every word of the topic appears in the headline, summary or source name."""
    words = _topic_words(topic)
    if not words:
        return True
    text = f"{record.get('headline', '')} {record.get('summary', '')} {record.get('source', '')}".lower()
    for word in words:
        # English words must start a word ("ai" must not match "said"); Hindi
        # vowel signs break \b, so non-English words match anywhere.
        found = re.search(rf"\b{re.escape(word)}", text) if word.isascii() else word in text
        if not found:
            return False
    return True


def matches_region(record: dict, region: str) -> bool:
    """The headline, summary or source names the region or one of its main cities."""
    return region_strength(record, region) > 0


def region_strength(record: dict, region: str) -> int:
    """2: the paper files it under the region (its source or URL section names it).
    1: the headline or summary only mentions it ("cook from Bihar dies in Bengaluru").
    0: unrelated."""
    names = REGION_ALIASES.get(region.strip().lower(), (region,))
    path = urlparse(record.get("link", "")).path.lower()
    for name in names:
        slug = name.lower().replace(" ", "-")
        if name.lower() in record.get("source", "").lower() or f"/{slug}/" in path:
            return 2
    return 1 if any(matches_topic(record, name) for name in names) else 0


# ---- same story, different paper
#
# Papers word one event differently ("Kerala governor's veg-only order sparks
# row" / "Kerala governor faces row over ban on non-veg food"), so exact or
# near-exact matching misses them. Instead, headlines are compared by the
# distinctive words they share: each word weighs more the rarer it is across
# the saved headlines, so "Siwan" and "firecracker" count and "India" barely
# does. Tuned on a day of real headlines: at STORY_SIMILARITY and above,
# pairs were the same event; below it, mostly different events on one theme.

STORY_SIMILARITY = 0.42
_HEADLINE_STOPWORDS = {
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "from", "by", "with", "and", "or", "but",
    "is", "are", "was", "were", "be", "been", "as", "its", "it", "this", "that", "over", "after",
    "before", "into", "about", "amid", "says", "say", "said", "new", "day", "vs",
    "के", "की", "का", "में", "से", "को", "पर", "ने", "और", "है", "हैं", "एक", "भी", "लिए",
}
_HEADLINE_PUNCTUATION = "\"'‘’“”:;,.!?()[]|-–—/…।"


def headline_words(headline: str) -> frozenset[str]:
    """The distinctive words of a headline. Split on spaces, not \\w: Hindi vowel
    signs would break words apart."""
    words = set()
    for raw in headline.lower().replace("’s", "").replace("'s", "").split():
        word = raw.strip(_HEADLINE_PUNCTUATION)
        if not word or word in _HEADLINE_STOPWORDS:
            continue
        if word.isascii() and len(word) > 3 and word.endswith("s"):
            word = word[:-1]
        words.add(word)
    return frozenset(words)


class _StoryMatcher:
    """Decides whether two headlines report the same event (see above)."""

    def __init__(self, records: list[dict]):
        self._words = {id(r): headline_words(r["headline"]) for r in records}
        counts: dict[str, int] = {}
        for words in self._words.values():
            for word in words:
                counts[word] = counts.get(word, 0) + 1
        total = max(len(records), 1)
        self._weight = {word: math.log(total / count) for word, count in counts.items()}

    def _weight_of(self, words) -> float:
        return sum(self._weight.get(word, 0.0) for word in words)

    def same_story(self, a: dict, b: dict) -> bool:
        if a.get("link") and a.get("link") == b.get("link"):
            return True
        ha, hb = a["headline"].lower(), b["headline"].lower()
        if ha.startswith(hb) or hb.startswith(ha):
            return True  # "Cyclone to hit Odisha" / "Cyclone to hit Odisha: schools shut"
        wa = self._words.get(id(a)) or headline_words(a["headline"])
        wb = self._words.get(id(b)) or headline_words(b["headline"])
        shared = wa & wb
        if len(shared) < 3:
            return False
        scale = math.sqrt(self._weight_of(wa) * self._weight_of(wb))
        return scale > 0 and self._weight_of(shared) / scale >= STORY_SIMILARITY


def search_news(
    topic: str | None = None,
    category: str | None = None,
    language: str | None = None,
    limit: int = 5,
    region: str | None = None,
) -> list[dict]:
    """The newest saved headlines matching the filters, one per story.

    Reads the last two days of saved headlines and drops anything older than
    MAX_AGE_HOURS. With no topic, region or category, only national ("india")
    headlines count, so a plain "what's the news?" gets the day's top stories.
    For a region, stories the paper files under it come before ones that only
    mention it; each event is shown once, from the paper that reported it last.
    """
    records = []
    for path in news_storage.get_recent_files(2):
        records.extend(news_storage.read_jsonl(path))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    epoch = datetime.min.replace(tzinfo=timezone.utc)

    pool = []
    for record in records:
        published = parse_time(record.get("timestamp"))
        if published and published < cutoff:
            continue
        if language and record.get("language") != language:
            continue
        record_category = category_of(record)
        if category and record_category != category:
            continue
        if not category and not topic and not region and record_category != "india":
            continue
        if topic and not matches_topic(record, topic):
            continue
        strength = region_strength(record, region) if region else 0
        if region and not strength:
            continue
        pool.append((strength, published or epoch, record))
    pool.sort(key=lambda entry: entry[:2], reverse=True)

    matcher = _StoryMatcher(records)
    picked: list[dict] = []
    for _, _, record in pool:
        if any(matcher.same_story(record, chosen) for chosen in picked):
            continue
        picked.append(record)
        if len(picked) >= limit:
            break
    return picked
