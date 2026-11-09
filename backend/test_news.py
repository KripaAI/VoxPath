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


def test_top_political_news_from_bihar(saved_news):
    # The real failure: the agent asked for region=Bihar, category=india, which hid
    # every Bihar paper, and "Atal Bihari Vajpayee" passed for a Bihar mention.
    saved_news(
        (1, "BJP chief asks opposition to follow Advani", "Times of India", "india", "en",
         "He credited Advani and Atal Bihari Vajpayee for the party's growth."),
        (2, "Congress harbours hostility towards Bihar: Saraogi", "Times of India Patna", "regional", "en", ""),
        (3, "Winter session of Bihar Vidhan Sabha to begin on Nov 30", "Times of India Patna", "regional", "en", ""),
        (4, "10 ancient idols stolen from Saran temple", "Times of India Patna", "regional", "en", ""),
        (5, "Bihar's Nitish Kumar meets the PM", "Hindustan Times", "india", "en", ""),
        (6, "पटना में तेजस्वी का सरकार पर हमला", "Amar Ujala Bihar", "regional", "hi", ""),
    )
    headlines = lambda **kw: [h["headline"] for h in services.get_latest_news(limit=10, **kw)["headlines"]]

    everything_from_bihar = headlines(region="Bihar")
    assert "BJP chief asks opposition to follow Advani" not in everything_from_bihar  # "Bihari" is not Bihar
    assert "Bihar's Nitish Kumar meets the PM" in everything_from_bihar
    assert headlines(region="Bihar", category="india") == everything_from_bihar  # category no longer hides Bihar papers

    # "politics" matches political headlines that never say the word, in both languages.
    assert headlines(region="Bihar", topic="politics") == [
        "Congress harbours hostility towards Bihar: Saraogi",
        "Winter session of Bihar Vidhan Sabha to begin on Nov 30",
        "पटना में तेजस्वी का सरकार पर हमला",
        "Bihar's Nitish Kumar meets the PM",
    ]
    assert "10 ancient idols stolen from Saran temple" not in headlines(region="Bihar", topic="political news")
    # The tool description offers "politics" as a category; it searches the same way.
    assert headlines(region="Bihar", category="politics") == headlines(region="Bihar", topic="politics")


def test_everyday_government_news_is_not_politics():
    # Real Bihar headlines the answers_question judge failed as "political news".
    record = lambda text: {"headline": text, "summary": "", "source": "Times of India Patna"}
    for not_politics in (
        "CM opens Deepotsav with over 11L lamps",
        "Govt sets 48 lakh hectares target for Rabi crops cultivation",
        "Early detection key to fighting cancer: Health minister",
        "Birthday party turns violent in Gaya",
    ):
        assert not news_crawler.matches_topic(record(not_politics), "politics"), not_politics
    for politics in (
        "Congress harbours hostility towards Bihar: Saraogi",
        "Five-day winter session of Bihar Vidhan Sabha to begin on Nov 30",
        "Bypoll dates announced for two Bihar seats",
    ):
        assert news_crawler.matches_topic(record(politics), "politics"), politics


def test_ndtv_profit_keeps_only_its_business_sections(saved_news):
    # NDTV Profit's feed is general news; only its business sections count as business.
    saved_news(
        (1, "4 Dead, 88 Injured In Egypt Train Crash", "NDTV Profit", "business", "en", ""),
        (2, "Muhurat Trading: Sun Pharma, Tata Steel Headline Gains", "NDTV Profit", "business", "en", ""),
        (3, "Where is Hardik Pandya? IPL Exit Rumours", "NDTV Profit", "business", "en", ""),
        (4, "Bosch bonus issue, stock split after 38% rally", "Mint Markets", "business", "en", ""),
    )
    path = news_storage.get_latest_file()
    records = news_storage.read_jsonl(path)
    for record, section in zip(records, ("world", "markets", "sports", "x")):
        record["link"] = f"https://www.ndtvprofit.com/{section}/story-{section}"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")

    assert [r["headline"] for r in news_crawler.search_news(category="business", limit=10)] == [
        "Muhurat Trading: Sun Pharma, Tata Steel Headline Gains",
        "Bosch bonus issue, stock split after 38% rally",  # other sources are not filtered
    ]

    feed = b"""<rss><channel>
      <item><title>Wall Street Races For Megadeals As Countdown Begins</title><link>https://www.ndtvprofit.com/markets/wall-street</link></item>
      <item><title>Ramayana Clips Leak Online Ahead Of The Release</title><link>https://www.ndtvprofit.com/entertainment/ramayana</link></item>
    </channel></rss>"""
    config = next(s for s in news_crawler.SOURCES if s["name"] == "NDTV Profit")
    assert [i["headline"] for i in news_crawler.parse_rss(feed, config)] == ["Wall Street Races For Megadeals As Countdown Begins"]

    # Its /business/ section also carries film box office: that is not market news.
    box_office = "https://www.ndtvprofit.com/business/entertainment-cinema-bollywood-movie-ramayana-box-office-collection-day-4"
    assert not news_crawler.in_wanted_section(box_office, config)
    assert news_crawler.in_wanted_section("https://www.ndtvprofit.com/business/dixon-working-capital", config)


def test_theme_words_match_whole_words_only():
    record = lambda text: {"headline": text, "summary": "", "source": "X"}
    assert news_crawler.matches_topic(record("MPs walk out of the House"), "politics")
    assert not news_crawler.matches_topic(record("Campus placement drive at IIT Patna"), "politics")  # "mp", "cm" inside words
    assert not news_crawler.matches_topic(record("Pollution levels rise in Patna"), "politics")  # "poll" is a whole word


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
