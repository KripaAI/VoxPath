"""Hindi (Devanagari) company names in the stock tool: transliteration, sound-alike
matching against NSE names, and the lookup order in services. Offline: a fixed
NSE list stands in for the real one and no price is fetched."""

import pytest

import hindi_names as h
import services
import stock_resolver

NSE = [
    ("RELIANCE", "RELIANCE INDUSTRIES LIMITED"),
    ("RALLIS", "RALLIS INDIA LIMITED"),
    ("INFY", "INFOSYS LIMITED"),
    ("IFCI", "IFCI LIMITED"),
    ("HDFCBANK", "HDFC BANK LIMITED"),
    ("HDFCLIFE", "HDFC LIFE INSURANCE COMPANY LIMITED"),
    ("WIPRO", "WIPRO LIMITED"),
    ("TMCV", "TATA MOTORS LIMITED"),
    ("TMPV", "TATA MOTORS PASSENGER VEHICLES LIMITED"),
    ("TATASTEEL", "TATA STEEL LIMITED"),
    ("TITAN", "TITAN COMPANY LIMITED"),
    ("BHARTIARTL", "BHARTI AIRTEL LIMITED"),
    ("MARUTI", "MARUTI SUZUKI INDIA LIMITED"),
    ("ASIANPAINT", "ASIAN PAINTS LIMITED"),
    ("HINDUNILVR", "HINDUSTAN UNILEVER LIMITED"),
    ("BAJFINANCE", "BAJAJ FINANCE LIMITED"),
    ("BAJAJFINSV", "BAJAJ FINSERV LIMITED"),
    ("SUNPHARMA", "SUN PHARMACEUTICAL INDUSTRIES LIMITED"),
    ("SPARC", "SUN PHARMA ADVANCED RESEARCH COMPANY LIMITED"),
    ("LT", "LARSEN & TOUBRO LIMITED"),
    ("ADANIENT", "ADANI ENTERPRISES LIMITED"),
    ("ADANIPORTS", "ADANI PORTS AND SPECIAL ECONOMIC ZONE LIMITED"),
    ("YESBANK", "YES BANK LIMITED"),
    ("KOTAKBANK", "KOTAK MAHINDRA BANK LIMITED"),
    ("ICICIBANK", "ICICI BANK LIMITED"),
    ("METROBRAND", "METRO BRANDS LIMITED"),
]


@pytest.fixture
def nse(monkeypatch):
    monkeypatch.setattr(stock_resolver, "_load", lambda: NSE)


@pytest.mark.parametrize("hindi, latin", [
    ("रिलायंस", "rilayans"),             # final inherent 'a' is silent; anusvara -> n
    ("इंफोसिस", "imphosis"),             # anusvara before a labial -> m
    ("टाटा मोटर्स", "tata motars"),       # half consonant (virama)
    ("ज़ोमैटो", "zomaito"),               # nukta: ज़ -> z
    ("एचडीएफसी बैंक", "HDFC baink"),      # letter names -> an abbreviation in capitals
    ("आईसीआईसीआई", "ICICI"),
    ("टीसीएस", "TCS"),
    ("रिलायंस का शेयर प्राइस क्या है?", "rilayans"),  # filler words dropped
    ("HDFC बैंक", "HDFC baink"),          # Latin words pass through
])
def test_to_latin(hindi, latin):
    assert h.to_latin(hindi) == latin


def test_a_hindi_word_is_not_mistaken_for_an_abbreviation():
    assert h.to_latin("एक") == "ek"  # starts with ए (the letter A) but isn't letter names
    assert h.to_latin("के") == ""     # a filler word, dropped


@pytest.mark.parametrize("hindi_sound, english", [
    ("rilayans", "RELIANCE"), ("imphosis", "INFOSYS"), ("vipro", "WIPRO"), ("eyaratel", "AIRTEL"),
    ("sujuki", "SUZUKI"), ("yunilivar", "UNILEVER"), ("phainens", "FINANCE"), ("motars", "MOTORS"),
])
def test_phonetic_keys_meet_in_the_middle(hindi_sound, english):
    assert h.phonetic_key(hindi_sound) == h.phonetic_key(english)


@pytest.mark.parametrize("hindi, symbol", [
    ("रिलायंस", "RELIANCE"),
    ("इंफोसिस", "INFY"),
    ("विप्रो", "WIPRO"),
    ("टाटा मोटर्स", "TMCV"),
    ("टाटा स्टील", "TATASTEEL"),   # "tata" must not be dropped as the noise word "THE" (same key)
    ("टाइटन", "TITAN"),
    ("भारती एयरटेल", "BHARTIARTL"),
    ("मारुति सुजुकी", "MARUTI"),
    ("एशियन पेंट्स", "ASIANPAINT"),
    ("हिंदुस्तान यूनिलीवर", "HINDUNILVR"),
    ("बजाज फाइनेंस", "BAJFINANCE"),
    ("सन फार्मा", "SUNPHARMA"),    # "pharma" is a short form of PHARMACEUTICAL
    ("लार्सन एंड टुब्रो", "LT"),
    ("अदानी एंटरप्राइजेज", "ADANIENT"),
    ("अदानी पोर्ट्स", "ADANIPORTS"),
    ("यस बैंक", "YESBANK"),
    ("कोटक महिंद्रा बैंक", "KOTAKBANK"),
    ("एचडीएफसी बैंक", "HDFCBANK"),  # abbreviation + word
    ("आईसीआईसीआई बैंक", "ICICIBANK"),
])
def test_nse_phonetic_matches_top_result(nse, hindi, symbol):
    assert stock_resolver.nse_phonetic_matches(h.to_latin(hindi))[:1] == [symbol]


def test_abbreviation_only_queries_are_left_to_the_exact_matcher(nse):
    assert stock_resolver.nse_phonetic_matches(h.to_latin("एचडीएफसी")) == []
    assert stock_resolver.nse_matches("HDFC")  # handled there, as for an English query


def test_hindi_name_is_priced_on_the_first_try(nse, monkeypatch):
    """The point of the change: no failed first lookup, so no retry by the agent."""
    tried = []

    def price(symbol):
        tried.append(symbol)
        return 1324.0 if symbol == "RELIANCE.NS" else None

    monkeypatch.setattr(services, "_price_for_symbol", price)
    assert services.get_stock_price("रिलायंस") == "RELIANCE INDUSTRIES LIMITED (RELIANCE.NS): 1324.00 INR"
    assert tried == ["RELIANCE.NS"]


def test_hindi_nifty_is_the_index(monkeypatch):
    monkeypatch.setattr(services, "_price_for_symbol", lambda s: 24986.4 if s == "^NSEI" else None)
    assert services.get_stock_price("निफ्टी").startswith("^NSEI: 24986.40")
    assert services.get_stock_price("निफ्टी 50").startswith("^NSEI")


def test_unknown_hindi_name_never_becomes_a_made_up_ticker(nse, monkeypatch):
    tried = []
    monkeypatch.setattr(services, "_price_for_symbol", lambda s: tried.append(s))
    monkeypatch.setattr(services.yf, "Search", lambda q: type("S", (), {"quotes": []})())
    assert "Could not find" in services.get_stock_price("क्षक्षक्ष")
    assert not any(s.startswith(("क", "KSHA")) for s in tried)
