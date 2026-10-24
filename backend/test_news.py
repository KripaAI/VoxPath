"""News for the agent: summaries, categories, topic search, and linkable citations."""

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import news_crawler
import news_storage
import services


def test_parse_rss_keeps_summary_category_and_publish_time():
    sample_rss = b"""<?xml version="1.0" encoding="UTF-8" ?>
    <rss version="2.0"><channel><item>
      <title>RBI keeps the repo rate unchanged at 6.5%</title>
      <link> https://example.com/rbi </link>
      <description><![CDATA[<p>The central bank held rates for a <b>fifth</b> meeting.</p>]]></description>
      <pubDate>Sat, 24 Oct 2026 12:43:38 +0530</pubDate>
    </item></channel></rss>"""
    config = {"name": "Test Business", "url": "https://example.com", "max_items": 5, "language": "en", "category": "business"}

    [item] = news_crawler.parse_rss(sample_rss, config)

    assert item["link"] == "https://example.com/rbi"
    assert item["summary"] == "The central bank held rates for a fifth meeting."
    assert item["category"] == "business"
    assert item["timestamp"] == "2026-10-24T12:43:38+05:30"


def test_summaries_are_cut_at_a_word():
    summary = news_crawler.summarize("word " * 200)
    assert len(summary) <= news_crawler.SUMMARY_CHARS + 1 and summary.endswith("word…")


@pytest.fixture
def saved_news(monkeypatch):
    """Point news storage at a temp folder; yields a function that saves headlines."""
    # Own temp dir rather than pytest's tmp_path (see test_fixes: its shared base
    # folder can be unwritable on Windows).
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        monkeypatch.setattr(news_storage, "DATA_DIR", tmp_path)
        yield _saver(tmp_path)


def _saver(tmp_path):
    now = datetime.now(timezone.utc)

    def save(*records):
        lines = []
        for minutes_ago, headline, source, category, language, summary in records:
            lines.append(json.dumps({
                "headline": headline, "link": f"https://example.com/{len(lines)}", "summary": summary,
                "source": source, "language": language, "category": category,
                "timestamp": (now - timedelta(minutes=minutes_ago)).isoformat(),
            }, ensure_ascii=False))
        (tmp_path / f"headlines_{now:%Y-%m-%d}.jsonl").write_text("\n".join(lines), encoding="utf-8")

    return save


def test_search_filters_by_category_topic_and_language(saved_news):
    saved_news(
        (1, "Sensex jumps 500 points as banks rally", "Mint Markets", "business", "en", ""),
        (2, "India beat New Zealand in the second T20", "NDTV Sports", "sports", "en", "A cricket thriller in Mumbai."),
        (3, "Parliament passes the new data protection bill", "The Hindu", "india", "en", ""),
        (4, "पटना में नई मेट्रो लाइन शुरू", "Amar Ujala Bihar", "regional", "hi", ""),
        (5, "He said the AI rules would change", "The Hindu", "india", "en", ""),
        (60 * 24 * 4, "A story from four days ago about cricket", "NDTV Sports", "sports", "en", ""),
    )
    headlines = lambda **kw: [r["headline"] for r in news_crawler.search_news(limit=10, **kw)]

    # No filters: national news only, newest first.
    assert headlines() == ["Parliament passes the new data protection bill", "He said the AI rules would change"]
    assert headlines(category="business") == ["Sensex jumps 500 points as banks rally"]
    # A topic matches the summary too, and old stories are dropped.
    assert headlines(topic="cricket news") == ["India beat New Zealand in the second T20"]
    # A region matches the source name; a whole word is needed ("ai" is not "said").
    assert headlines(topic="Bihar") == ["पटना में नई मेट्रो लाइन शुरू"]
    assert headlines(topic="AI") == ["He said the AI rules would change"]
    assert headlines(topic="rules", language="hi") == []


def test_region_finds_the_state_and_its_main_city(saved_news):
    saved_news(
        (1, "Parliament passes the new data protection bill", "The Hindu", "india", "en", ""),
        (2, "Dhaba owner shot at in Samastipur", "Times of India Patna", "regional", "en", ""),
        (3, "Nitish Kumar reviews flood relief in Bihar", "Hindustan Times", "india", "en", ""),
        (4, "पटना में नई मेट्रो लाइन शुरू", "Amar Ujala Bihar", "regional", "hi", ""),
    )
    headlines = lambda **kw: [r["headline"] for r in news_crawler.search_news(limit=10, **kw)]

    # Bihar papers first (newest first), then national stories that mention Bihar.
    assert headlines(region="Bihar") == [
        "Dhaba owner shot at in Samastipur",
        "पटना में नई मेट्रो लाइन शुरू",
        "Nitish Kumar reviews flood relief in Bihar",
    ]
    assert headlines(region="bihar", language="hi") == ["पटना में नई मेट्रो लाइन शुरू"]


def test_region_section_in_the_url_counts_as_filed_under_it(saved_news):
    saved_news(
        (1, "Cook from Bihar dies after being hit by a car in Bengaluru", "The Hindu", "india", "en", ""),
        (2, "Bihar attracts 1,091 investment proposals", "The Hindu", "india", "en", ""),
    )
    records = news_storage.read_jsonl(news_storage.get_latest_file())
    records[0]["link"] = "https://www.thehindu.com/news/cities/bangalore/cook-from-bihar-dies/article1.ece"
    records[1]["link"] = "https://www.thehindu.com/news/national/bihar/bihar-attracts-investment/article2.ece"

    assert [news_crawler.region_strength(r, "Bihar") for r in records] == [1, 2]
    assert news_crawler.region_strength(records[0], "Kerala") == 0


def test_region_news_in_another_language_beats_unrelated_news(saved_news):
    saved_news(
        (1, "Parliament passes the new data protection bill", "The Hindu", "india", "en", ""),
        (2, "पटना में नई मेट्रो लाइन शुरू", "Amar Ujala Bihar", "regional", "hi", ""),
    )

    # No English Bihar news: Hindi Bihar news, not national news, and a note.
    result = services.get_latest_news(region="Bihar", language="en")
    assert [h["source"] for h in result["headlines"]] == ["Amar Ujala Bihar"]
    assert "No recent headlines matched news from Bihar in English. These are news from Bihar instead" in result["note"]

    result = services.get_latest_news(region="Kerala")
    assert [h["source"] for h in result["headlines"]] == ["The Hindu"]
    assert "matched news from Kerala. These are top national news instead" in result["note"]


def test_search_shows_each_story_once(saved_news):
    saved_news(
        (1, "Cyclone to hit Odisha coast on October 28", "The Hindu", "india", "en", ""),
        (2, "Cyclone to hit Odisha coast on October 28, schools shut", "NDTV News", "india", "en", ""),
        # Same event, different wording (real headlines from one morning).
        (3, "Kerala Governor's Veg-Only Order For Lok Bhavan Kitchen On Day 1 Sparks Row", "NDTV News", "india", "en", ""),
        (4, "Kerala governor Om Prakash Mathur's 'veg-only kitchen' diktat on first day sparks row", "Times of India", "india", "en", ""),
        (5, "At least four dead in blast in Bihar's Siwan district", "NDTV News", "india", "en", ""),
        (6, "At least four killed in blast at house storing firecrackers in Bihar's Siwan", "The Hindu", "india", "en", ""),
        # Different events that share a theme stay apart.
        (7, "Parliament passes the new data protection bill", "The Hindu", "india", "en", ""),
        (8, "Opposition walks out of Parliament over voter list row", "Hindustan Times", "india", "en", ""),
        (9, "Delhi Police arrest gangster in Dwarka shootout", "NDTV News", "india", "en", ""),
        (10, "Mumbai Police bust drug racket at Andheri port", "Times of India", "india", "en", ""),
    )
    shown = [r["headline"] for r in news_crawler.search_news(limit=10)]
    assert shown == [
        "Cyclone to hit Odisha coast on October 28",
        "Kerala Governor's Veg-Only Order For Lok Bhavan Kitchen On Day 1 Sparks Row",
        "At least four dead in blast in Bihar's Siwan district",
        "Parliament passes the new data protection bill",
        "Opposition walks out of Parliament over voter list row",
        "Delhi Police arrest gangster in Dwarka shootout",
        "Mumbai Police bust drug racket at Andheri port",
    ]


def test_agent_news_items_carry_url_time_and_summary(saved_news):
    saved_news((1, "India beat New Zealand in the second T20", "NDTV Sports", "sports", "en", "A cricket thriller."))

    [item] = services.get_latest_news(category="sports")["headlines"]

    assert item["url"] == "https://example.com/0"
    assert item["source"] == "NDTV Sports"
    assert item["summary"] == "A cricket thriller."
    assert item["published"].endswith("IST")


def test_unmatched_topic_says_so_instead_of_pretending(saved_news):
    saved_news(
        (1, "Parliament passes the new data protection bill", "The Hindu", "india", "en", ""),
        (2, "India beat New Zealand in the second T20", "NDTV Sports", "sports", "en", ""),
    )

    result = services.get_latest_news(topic="quidditch", category="sports")
    assert [h["category"] for h in result["headlines"]] == ["sports"]
    assert "No recent headlines matched 'quidditch' sports news. These are sports news instead" in result["note"]

    # An unknown category is searched for as a topic instead.
    assert services.get_latest_news(category="parliament")["headlines"][0]["headline"].startswith("Parliament")
    # A plain request has nothing to apologise for.
    assert "note" not in services.get_latest_news()
