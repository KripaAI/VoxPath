import os
import re
from datetime import timedelta, timezone
from typing import Any, Iterator

import yfinance as yf
from dotenv import load_dotenv
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

import news_config
import news_crawler
import stock_resolver
from logging_config import get_logger

load_dotenv()

log = get_logger("voxpath.services")


def build_llm() -> BaseChatModel:
    """Build the chat model used by the text agent.

    Uses OpenAI, authenticated with OPENAI_API_KEY from the environment. The
    model is set by OPENAI_MODEL.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Add it to backend/.env to use the chat agent."
        )
    return ChatOpenAI(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        api_key=api_key,
        temperature=0,

        max_retries=2,
    )





EMBEDDING_DIMS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"


def get_embedding_model() -> str:
    return os.getenv("OPENAI_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)


def get_embedding_dims() -> int:
    """Vector width for the configured embedding model.

    Unknown models must declare OPENAI_EMBEDDING_DIMS explicitly rather than
    silently defaulting, since a wrong width fails at query time, not here.
    """
    model = get_embedding_model()
    override = os.getenv("OPENAI_EMBEDDING_DIMS")
    if override:
        return int(override)
    try:
        return EMBEDDING_DIMS[model]
    except KeyError:
        raise RuntimeError(
            f"Unknown embedding model {model!r}. Set OPENAI_EMBEDDING_DIMS to its vector width, or use one of: "
            + ", ".join(sorted(EMBEDDING_DIMS))
        ) from None


def build_embeddings() -> OpenAIEmbeddings:
    """Build the embedding model backing long-term memory search.

    Uses OpenAI, authenticated with OPENAI_API_KEY, same as build_llm(). The
    model is set by OPENAI_EMBEDDING_MODEL.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Add it to backend/.env to use memory search."
        )
    return OpenAIEmbeddings(model=get_embedding_model(), api_key=api_key)


def get_market_keywords() -> list[str]:
    return ["stock price", "stock market", "price of", "nifty", "sensex", "shares of", "ticker"]


def get_news_keywords() -> list[str]:
    return ["news", "headline", "headlines", "crawl", "crawler", "newspaper", "rss", "bulletin"]


def route_message(message: str) -> str:
    lowered = message.lower()

    if any(keyword in lowered for keyword in get_news_keywords()):
        return "news_agent"
    if any(keyword in lowered for keyword in get_market_keywords()) or "share" in lowered:




        if "share" in lowered and not any(k in lowered for k in ("price", "stock", "market")):


            pass
        else:
            return "market_agent"

    if any(keyword in lowered for keyword in get_market_keywords()):
        return "market_agent"

    return "general_agent"


def normalize_ticker(ticker: str) -> str:
    if ticker.endswith(".NS") or ticker.endswith(".BO") or ticker.startswith("^"):
        return ticker
    return f"{ticker.upper()}.NS"


def _price_for_symbol(symbol: str) -> float | None:
    """Best-effort live/last price for a fully-qualified Yahoo symbol."""
    try:
        ticker = yf.Ticker(symbol)
        try:
            price = ticker.fast_info.last_price
            if price is not None:
                return float(price)
        except Exception:
            pass
        history = ticker.history(period="5d")
        if not history.empty:
            return float(history["Close"].iloc[-1])
    except Exception as error:
        log.debug("price lookup failed for %s: %s", symbol, error)
    return None


def _candidate_symbols(query: str) -> Iterator[str]:
    """Resolve a company name or ticker to ordered Yahoo symbol candidates.

    Uses Yahoo search so any listed company works (and so ticker changes like
    TATAMOTORS -> TMCV/TMPV resolve to the current symbol), preferring NSE then BSE.

    A generator: get_stock_price stops at the first candidate with a price, so the
    Yahoo search (~1.3 s) only runs when no NSE match could be priced.
    """
    q = query.strip()
    seen: set[str] = set()
    listed = False

    def fresh(symbols):
        nonlocal listed
        for symbol in symbols:
            if symbol not in seen:
                seen.add(symbol)
                listed = listed or symbol.endswith((".NS", ".BO"))
                yield symbol

    if q.endswith((".NS", ".BO")) or q.startswith("^"):
        yield from fresh([q])

    yield from fresh(f"{sym}.NS" for sym in stock_resolver.nse_matches(q))

    quotes = []
    try:
        quotes = yf.Search(q).quotes
    except Exception as error:
        log.debug("symbol search failed for %r: %s", q, error)
    yield from fresh([r["symbol"] for r in quotes if r.get("symbol", "").endswith(".NS")])
    yield from fresh([r["symbol"] for r in quotes if r.get("symbol", "").endswith(".BO")])

    token = q.upper().replace(" ", "")
    if token and not listed:
        yield from fresh([f"{token}.NS", f"{token}.BO"])


def get_stock_price(query: str) -> str:
    """Live price for any Indian-listed stock by company name OR ticker."""
    if not query or not query.strip():
        return "Please specify a stock or company name."
    q = query.strip()
    if q.upper() in ("NIFTY", "NIFTY50", "NIFTY 50"):
        q = "^NSEI"

    for symbol in _candidate_symbols(q):
        price = _price_for_symbol(symbol)
        if price is not None:
            log.info("stock %r -> %s = %.2f", query, symbol, price)
            # Name the company so a near-match (e.g. SBI -> SBI Funds) is visible, not silent.
            name = stock_resolver.company_name(symbol)
            return f"{name} ({symbol}): {price:.2f} INR" if name else f"{symbol}: {price:.2f} INR"

    log.warning("no price found for %r", query)
    return f"Could not find live price data for '{query}'."


def extract_market_tickers(query: str, llm: BaseChatModel | None = None) -> list[str]:
    llm = llm or build_llm()
    extract_prompt = (
        f"Extract Indian stock tickers from this query: '{query}'. "
        "Return only a comma-separated list of symbols (e.g. TCS, INFY, RELIANCE). Use 'INFY' for Infosys. If none, return 'NIFTY'."
    )
    log.debug("extracting tickers via LLM for: %r", query[:80])
    llm_response = llm.invoke(extract_prompt)
    ticker_text = str(llm_response.content).strip()
    tickers = [ticker.strip() for ticker in ticker_text.split(",") if ticker.strip()]
    log.info("extracted tickers: %s", tickers)
    return tickers


def market_report(query: str, llm: BaseChatModel | None = None) -> dict[str, Any]:
    tickers = extract_market_tickers(query, llm=llm)
    results = [get_stock_price(ticker if ticker != "NIFTY" else "^NSEI") for ticker in tickers]
    report = "\n".join(results)
    return {
        "agent": "market_agent",
        "tickers": tickers,
        "report": report,
        "text": f"Indian Stock Market Update\n\n{report}",
    }


def general_chat(message: str, llm: BaseChatModel | None = None) -> dict[str, str]:
    llm = llm or build_llm()
    log.debug("general chat LLM invoke: %r", message[:80])
    response = llm.invoke(message)
    return {
        "agent": "general_agent",
        "text": str(response.content),
    }


def handle_chat(message: str, llm: BaseChatModel | None = None) -> dict[str, Any]:
    selected_agent = route_message(message)
    log.info("routed to %s: %r", selected_agent, message[:80])
    if selected_agent == "market_agent":
        return market_report(message, llm=llm)
    if selected_agent == "news_agent":
        return news_chat(message)
    return general_chat(message, llm=llm)


def list_news_sources() -> list[dict]:
    return news_crawler.list_news_sources()


def crawl_news(source_query: str | None = None, dry_run: bool = False, limit: int | None = None) -> dict[str, Any]:
    return news_crawler.crawl_news(source_query=source_query, dry_run=dry_run, limit=limit)


def get_latest_headlines(limit: int = 10, source: str | None = None, language: str | None = None) -> dict[str, Any]:
    return news_crawler.get_latest_headlines(limit=limit, source=source, language=language)


IST = timezone(timedelta(hours=5, minutes=30))
_LANGUAGES = {"en": "en", "english": "en", "hi": "hi", "hindi": "hi"}


def _news_item(record: dict) -> dict[str, str]:
    """One headline as the agent cites it: linked, dated, with a summary when the feed gave one."""
    item = {"headline": record["headline"], "source": record["source"], "url": record.get("link", "")}
    published = news_crawler.parse_time(record.get("timestamp"))
    if published:
        local = published.astimezone(IST)
        item["published"] = f"{local.day} {local:%b}, {local.hour % 12 or 12}:{local:%M %p} IST"
    if record.get("summary"):
        item["summary"] = record["summary"]
    item["category"] = news_crawler.category_of(record)
    return item


def _describe_news_filters(filters: dict[str, str | None]) -> str:
    """e.g. "'cricket' sports news from Bihar in Hindi"."""
    parts = [
        filters["topic"] and f"'{filters['topic']}'",
        f"{filters['category']} news" if filters["category"] else "news",
        filters["region"] and f"from {filters['region']}",
        filters["language"] and f"in {'Hindi' if filters['language'] == 'hi' else 'English'}",
    ]
    return " ".join(filter(None, parts))


def get_latest_news(
    topic: str | None = None,
    category: str | None = None,
    language: str | None = None,
    limit: int = 5,
    region: str | None = None,
) -> dict[str, Any]:
    """Latest saved headlines for the agent, filtered to what the user wants.

    Each item carries its article URL and publish time so answers can cite and
    link the source. When nothing matches, filters are relaxed one at a time
    (language, then topic, category, region) and a `note` says what could not
    be found, rather than passing off unrelated news as a match.
    """
    category = (category or "").strip().lower() or None
    topic = (topic or "").strip() or None
    if category and category not in news_config.CATEGORIES:
        topic = " ".join(filter(None, [topic, category]))  # e.g. "cricket": search for it instead
        category = None
    filters = {
        "topic": topic,
        "region": (region or "").strip() or None,
        "category": category,
        "language": _LANGUAGES.get((language or "").strip().lower()),
    }
    limit = max(1, min(int(limit or 5), 10))

    def search() -> list[dict]:
        return news_crawler.search_news(limit=limit, **filters)

    records = search()
    if not records and not news_crawler.search_news(limit=1):
        # Nothing saved yet (first run): crawl once, then search again.
        try:
            crawl_news(source_query=None)
        except Exception as error:
            log.warning("news fallback crawl failed: %s", error)
        records = search()

    result: dict[str, Any] = {}
    if not records:
        asked = _describe_news_filters(filters)
        for loosen in ("language", "topic", "category", "region"):
            if not filters[loosen]:
                continue
            filters[loosen] = None
            records = search()
            if records:
                shown = _describe_news_filters(filters)
                shown = "top national news" if shown == "news" else shown
                log.info("news %r matched nothing, showing %s", asked, shown)
                result["note"] = (
                    f"No recent headlines matched {asked}. These are {shown} instead; "
                    "tell the user that, and don't describe them as what was asked for."
                )
                break
    items = [_news_item(record) for record in records]
    result.update(headlines=items, count=len(items))
    return result


def get_stock_price_smart(ticker: str) -> dict[str, str]:
    """Stock price by company name or ticker (handles the NIFTY index alias)."""
    return {"result": get_stock_price(ticker or "NIFTY")}


def detect_news_source_query(message: str) -> str | None:
    lowered = message.lower()
    alias_map = {
        "bihar": "Bihar",
        "uttar pradesh": "Uttar Pradesh",
        "uttar-pradesh": "Uttar Pradesh",
        "up news": "Uttar Pradesh",
        "up election": "Uttar Pradesh",
        "यूपी": "Uttar Pradesh",
        "उत्तर प्रदेश": "Uttar Pradesh",
    }
    for alias, canonical in alias_map.items():
        if alias in lowered:
            return canonical
    for source in news_crawler.list_news_sources():
        if source["name"].lower() in lowered:
            return source["name"]
    return None


def detect_news_language(message: str) -> str | None:
    lowered = message.lower()
    if "hindi" in lowered:
        return "hi"
    if "english" in lowered:
        return "en"
    return None


def detect_requested_headline_limit(message: str, default: int = 5, maximum: int = 25) -> int:
    match = re.search(r"\b(?:top|latest|give me|show me|get me)?\s*(\d{1,2})\b", message.lower())
    if not match:
        return default
    return max(1, min(int(match.group(1)), maximum))


def format_headline_list(items: list[dict], title: str = "Latest headlines:") -> str:
    lines = [title, ""]
    lines.extend(f"- {item['headline']} ({item['source']})" for item in items)
    return "\n".join(lines)


def news_chat(message: str) -> dict[str, Any]:
    lowered = message.lower()
    source_query = detect_news_source_query(message)
    language = detect_news_language(message)
    limit = detect_requested_headline_limit(message)

    if "crawl" in lowered or "fetch" in lowered or "run" in lowered:
        result = crawl_news(source_query=source_query, dry_run=False, limit=limit)
        if result["status"] == "no_match":
            return {"agent": "news_agent", "text": result["message"], "data": result}

        summary_lines = [
            f"News crawl complete. Saved {result['saved']} new headlines from {len(result['summary'])} sources."
        ]
        if source_query:
            summary_lines.append(f"Filtered source: {source_query}")
        if result["headlines"]:
            summary_lines.append("")
            summary_lines.append(format_headline_list(result["headlines"][:limit], title="Top saved headlines:"))
        return {
            "agent": "news_agent",
            "text": "\n".join(summary_lines),
            "data": result,
        }

    latest = get_latest_headlines(limit=limit, source=source_query, language=language)
    if latest["count"] == 0:
        guidance = "No stored headlines found yet. Ask me to crawl the news first."
        if source_query:
            guidance = f"No stored headlines found for {source_query}. Ask me to crawl that source first."
        return {"agent": "news_agent", "text": guidance, "data": latest}

    return {
        "agent": "news_agent",
        "text": format_headline_list(latest["headlines"]),
        "data": latest,
    }


def build_fallback_text(user_text: str | None) -> str:
    received_text = user_text.strip() if user_text else ""
    if received_text:
        return (
            "I received your message, but the live AI service is currently unavailable due to API quota limits. "
            f"Your message was: '{received_text}'. Please try again later or switch to a key with active quota."
        )
    return (
        "I could not reach the live AI service because the API quota is exhausted. "
        "Please try again later or switch to a key with active quota."
    )
