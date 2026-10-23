import services

def test_route_message_routes_market_queries():
    assert services.route_message("What is the stock price of TCS today?") == "market_agent"


def test_route_message_routes_news_queries():
    assert services.route_message("crawl latest news headlines") == "news_agent"


def test_route_message_routes_general_queries():
    assert services.route_message("Hello there") == "general_agent"


def test_route_message_misroutes_share_news():

    assert services.route_message("share today's news") == "news_agent"


def test_normalize_ticker():
    assert services.normalize_ticker("TCS") == "TCS.NS"
    assert services.normalize_ticker("RELIANCE.NS") == "RELIANCE.NS"
    assert services.normalize_ticker("^NSEI") == "^NSEI"


def test_detect_requested_headline_limit():
    assert services.detect_requested_headline_limit("give me 10 headlines") == 10
    assert services.detect_requested_headline_limit("show me latest news") == 5
    assert services.detect_requested_headline_limit("top 50 news", maximum=25) == 25


def test_stock_resolver_stemming():
    import stock_resolver

    assert stock_resolver._stem_word("TECHNOLOGIES") == "TECHNOLOGY"
    assert stock_resolver._stem_word("TECHNOLOGY") == "TECHNOLOGY"
    assert stock_resolver._stem_word("MOTORS") == "MOTOR"
    assert stock_resolver._stem_word("MOTOR") == "MOTOR"
    assert stock_resolver._stem_word("INDUSTRIES") == "INDUSTRY"
    assert stock_resolver._stem_word("CHEMICALS") == "CHEMICAL"
    assert stock_resolver._stem_word("SERVICES") == "SERVICE"
    assert stock_resolver._stem_word("ENTERPRISES") == "ENTERPRISE"


def test_nse_matches_near_similar_words(monkeypatch):
    import stock_resolver

    mock_rows = [
        ("TATATECH", "TATA TECHNOLOGIES LIMITED"),
        ("TMCV", "TATA MOTORS LIMITED"),
        ("TMPV", "TATA MOTORS PASSENGER VEHICLES LIMITED"),
        ("RELIANCE", "RELIANCE INDUSTRIES LIMITED"),
        ("TATACHEM", "TATA CHEMICALS LIMITED"),
        ("TCS", "TATA CONSULTANCY SERVICES LIMITED"),
        ("LT", "LARSEN & TOUBRO LIMITED"),
        ("LTF", "L&T FINANCE LIMITED"),
        ("ICICIBANK", "ICICI BANK LIMITED"),
        ("ADANIENT", "ADANI ENTERPRISES LIMITED"),
    ]
    monkeypatch.setattr(stock_resolver, "_load", lambda: mock_rows)

    assert stock_resolver.nse_matches("Tata technology")[0] == "TATATECH"
    assert stock_resolver.nse_matches("Tata Technologies")[0] == "TATATECH"
    assert stock_resolver.nse_matches("TATATECH") == ["TATATECH"]
    assert "TMCV" in stock_resolver.nse_matches("Tata motor")
    assert "TMCV" in stock_resolver.nse_matches("Tata motors")
    assert "TMCV" in stock_resolver.nse_matches("TATAMOTORS")
    assert "RELIANCE" in stock_resolver.nse_matches("Reliance industry")
    assert "RELIANCE" in stock_resolver.nse_matches("Reliance industries")
    assert "TATACHEM" in stock_resolver.nse_matches("Tata chemical")
    assert "TATATECH" in stock_resolver.nse_matches("Tata tecnology")
    assert "TMCV" in stock_resolver.nse_matches("Tata moter")
    assert stock_resolver.nse_matches("Larsen and toubro")[0] == "LT"
    assert stock_resolver.nse_matches("L&T") == ["LT"]
    assert stock_resolver.nse_matches("ICICI bnk")[0] == "ICICIBANK"
    assert stock_resolver.nse_matches("Adani enterprise")[0] == "ADANIENT"
    assert stock_resolver.nse_matches("Tata consultancy service")[0] == "TCS"


def test_stock_price_names_the_matched_company(monkeypatch):
    import stock_resolver

    monkeypatch.setattr(stock_resolver, "_load", lambda: [("SBIFUNDS", "SBI FUNDS MANAGEMENT LIMITED")])
    monkeypatch.setattr(services, "_candidate_symbols", lambda q: ["SBIFUNDS.NS"])
    monkeypatch.setattr(services, "_price_for_symbol", lambda s: 455.1)
    assert services.get_stock_price("SBI") == "SBI FUNDS MANAGEMENT LIMITED (SBIFUNDS.NS): 455.10 INR"

