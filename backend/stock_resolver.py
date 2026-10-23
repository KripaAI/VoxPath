"""Resolve a company name or ticker to its official NSE symbol.

Fetches NSE's official equity master list from the net (cached), so any of the
~2,300 NSE-listed companies can be found by official name or symbol — no
hardcoded aliases. Used as the primary source by services.get_stock_price,
with Yahoo search as a secondary source for renames/demergers and BSE-only names.
"""

from __future__ import annotations

import csv
import difflib
import io
import re
import threading
import time

import requests

from logging_config import get_logger

log = get_logger("voxpath.stocks")

NSE_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}
TTL_SECONDS = 86400
# After a failed download, wait this long before trying again. Without it every
# stock question would re-attempt the download and stall on its 15 s timeout.
RETRY_SECONDS = 300

_rows: list[tuple[str, str]] = []
_fetched_at = 0.0
_last_attempt = 0.0
_lock = threading.Lock()

_NOISE = {"LIMITED", "LTD", "LTD.", "THE", "INDIA", "CO", "CORP", "CORPORATION", "COMPANY", "AND", "&", "OF"}


def _clean_word(word: str) -> str:
    return word.strip(".,()&-_'\"").upper()


def _stem_word(word: str) -> str:
    """Normalize common English plurals and variations in company names."""
    w = _clean_word(word)
    if w.endswith("IES") and len(w) > 4:
        return w[:-3] + "Y"
    # Only a plain trailing S: stripping "ES" would turn SERVICES/ENTERPRISES into SERVIC/ENTERPRIS.
    if w.endswith("S") and len(w) > 3 and not w.endswith(("SS", "US", "IS")):
        return w[:-1]
    return w


def _word_match(qw: str, tw: str) -> float:
    """Similarity between a query word and a company name word.

    Returns:
        1.0 for exact matches,
        0.95 for stem matches (e.g. technology vs technologies, motor vs motors),
        0.70-0.90 for fuzzy/typo near-matches (e.g. tecnology, moter),
        0.0 for non-matches.
    """
    qw_c, tw_c = _clean_word(qw), _clean_word(tw)
    if qw_c == tw_c:
        return 1.0
    s1, s2 = _stem_word(qw_c), _stem_word(tw_c)
    if s1 == s2:
        return 0.95
    # Direct similarity on words (require same first char for performance)
    if len(qw_c) >= 4 and len(tw_c) >= 4 and abs(len(qw_c) - len(tw_c)) <= 2:
        if qw_c[0] == tw_c[0]:
            r = difflib.SequenceMatcher(None, qw_c, tw_c).ratio()
            if r >= 0.80:
                return 0.70 + 0.20 * r
    # Similarity on stemmed words
    if len(s1) >= 4 and len(s2) >= 4 and abs(len(s1) - len(s2)) <= 2:
        if s1[0] == s2[0]:
            r = difflib.SequenceMatcher(None, s1, s2).ratio()
            if r >= 0.80:
                return 0.70 + 0.20 * r
    # Short words (e.g. BNK vs BANK): stricter, since 3 letters leave little room for error
    if min(len(qw_c), len(tw_c)) == 3 and abs(len(qw_c) - len(tw_c)) <= 1 and qw_c[0] == tw_c[0]:
        r = difflib.SequenceMatcher(None, qw_c, tw_c).ratio()
        if r >= 0.85:
            return 0.70 + 0.20 * r
    return 0.0


def _load() -> list[tuple[str, str]]:
    """Return the cached NSE equity list, refreshing from the net if stale.

    If a refresh fails, the last good list keeps being served (an empty list if
    there never was one) and no new attempt is made for RETRY_SECONDS.
    """
    global _rows, _fetched_at, _last_attempt
    with _lock:
        now = time.time()
        if _rows and now - _fetched_at < TTL_SECONDS:
            return _rows
        if now - _last_attempt < RETRY_SECONDS:
            return _rows
        _last_attempt = now
        try:
            response = requests.get(NSE_URL, headers=_HEADERS, timeout=15)
            response.raise_for_status()
            reader = csv.reader(io.StringIO(response.text))
            next(reader, None)
            rows = [
                (row[0].strip(), row[1].strip().upper())
                for row in reader
                if len(row) >= 2 and row[0].strip()
            ]
            if rows:
                _rows, _fetched_at = rows, time.time()
                log.info("loaded %d NSE-listed symbols", len(rows))
        except Exception as error:
            log.warning("NSE equity list fetch failed: %s", error)
        return _rows


def nse_matches(query: str, limit: int = 3) -> list[str]:
    """Return up to `limit` NSE symbols matching a company name or ticker."""
    rows = _load()
    if not rows:
        return []

    q = query.strip().upper()
    compact = q.replace(" ", "")
    alnum = re.sub(r"[^A-Z0-9]", "", q)

    for symbol, _name in rows:
        if symbol == compact:
            return [symbol]
    # Same symbol written with punctuation, e.g. "L&T" for LT
    for symbol, _name in rows:
        if alnum and re.sub(r"[^A-Z0-9]", "", symbol) == alnum:
            return [symbol]

    q_words = [w for w in q.split() if w not in _NOISE and _clean_word(w)]
    scored = []
    for symbol, name in rows:
        name_words = [w for w in name.split() if w not in _NOISE and _clean_word(w)]
        compact_name = "".join(name_words)

        if name == q:
            score = 100.0
        elif compact and compact == compact_name:
            score = 90.0
        elif name.startswith(q + " "):
            score = 85.0
        elif q in name:
            score = 65.0
        elif q_words:
            # Check exact or near word matching
            # Each query word must match a distinct target word in the company name
            matched_indices: set[int] = set()
            word_scores: list[float] = []
            for qw in q_words:
                best_match = 0.0
                best_idx = -1
                for idx, tw in enumerate(name_words):
                    if idx in matched_indices:
                        continue
                    m = _word_match(qw, tw)
                    if m > best_match:
                        best_match = m
                        best_idx = idx
                if best_idx != -1 and best_match > 0.0:
                    matched_indices.add(best_idx)
                    word_scores.append(best_match)

            if len(word_scores) == len(q_words):
                min_m = min(word_scores)
                avg_m = sum(word_scores) / len(word_scores)
                if min_m == 1.0:
                    score = 55.0
                elif min_m >= 0.95:
                    score = 50.0
                else:
                    score = 40.0 * avg_m
            else:
                score = 0.0
        else:
            score = 0.0

        if score > 0:
            scored.append((score, len(name), symbol))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return [symbol for _score, _len, symbol in scored[:limit]]


def company_name(symbol: str) -> str | None:
    """Official NSE company name for a symbol like 'TATATECH' or 'TATATECH.NS', if listed."""
    base = symbol.upper().removesuffix(".NS").removesuffix(".BO")
    for sym, name in _load():
        if sym == base:
            return name
    return None
