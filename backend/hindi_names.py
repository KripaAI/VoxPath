"""Turn company names written in Hindi (Devanagari) into something the stock lookup can match.

Two steps, both plain rules, no network and no extra packages:

1. to_latin(): Devanagari -> Latin letters, word by word.
     रिलायंस -> "rilayans"      इंफोसिस -> "infosis"      टाटा मोटर्स -> "tata motars"
   A word made only of English letter names is an abbreviation and comes out in capitals:
     एचडीएफसी -> "HDFC"         टीसीएस -> "TCS"            एसबीआई -> "SBI"
   Filler words a user might include ("का", "शेयर", "प्राइस", "भाव") are dropped.

2. phonetic_key(): a sound-alike key that ignores how English spells a sound, so the
   transliteration and the official English name meet in the middle:
     "rilayans" and "RELIANCE" -> "rlns"     "vipro" and "WIPRO" -> "vpr"
   Vowels after the first letter are dropped (Hindi and English spell them differently),
   and c/k/q, w/v, z/j, ph/f, sh/s... are folded together.

stock_resolver.nse_phonetic_matches() compares these keys with NSE company names.
"""

from __future__ import annotations

import re

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")

_CONSONANTS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m",
    "य": "y", "र": "r", "ल": "l", "ळ": "l", "व": "v",
    "श": "sh", "ष": "sh", "स": "s", "ह": "h",
    # precomposed nukta letters
    "क़": "q", "ख़": "kh", "ग़": "g", "ज़": "z", "ड़": "r", "ढ़": "rh", "फ़": "f", "य़": "y",
}
# consonant + nukta (़) written as two characters
_NUKTA_FORMS = {"क": "q", "ख": "kh", "ग": "g", "ज": "z", "ड": "r", "ढ": "rh", "फ": "f", "य": "y"}
_NUKTA = "़"
_VIRAMA = "्"

_VOWELS = {
    "अ": "a", "आ": "a", "इ": "i", "ई": "i", "उ": "u", "ऊ": "u", "ऋ": "ri",
    "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au", "ऑ": "o", "ॲ": "e",
}
_VOWEL_SIGNS = {
    "ा": "a", "ि": "i", "ी": "i", "ु": "u", "ू": "u", "ृ": "ri",
    "े": "e", "ै": "ai", "ो": "o", "ौ": "au", "ॉ": "o", "ॅ": "e",
}
_LABIALS = {"प", "फ", "ब", "भ", "म"}
_DIGITS = {chr(0x0966 + d): str(d) for d in range(10)}

# How English letters are spoken in Hindi, for abbreviations (एचडीएफसी = H-D-F-C).
_LETTER_NAMES = {
    "ए": "A", "बी": "B", "सी": "C", "डी": "D", "ई": "E", "एफ": "F", "एफ़": "F", "जी": "G",
    "एच": "H", "आई": "I", "जे": "J", "के": "K", "एल": "L", "एम": "M", "एन": "N", "ओ": "O",
    "पी": "P", "क्यू": "Q", "आर": "R", "एस": "S", "टी": "T", "यू": "U", "वी": "V",
    "डब्ल्यू": "W", "डबल्यू": "W", "एक्स": "X", "वाई": "Y", "ज़ेड": "Z", "जेड": "Z",
}
_LETTER_NAMES_LONGEST_FIRST = sorted(_LETTER_NAMES, key=len, reverse=True)

# Words people add around a company name ("रिलायंस का शेयर प्राइस क्या है").
_FILLER = {
    "का", "की", "के", "को", "में", "है", "हैं", "क्या", "कितना", "कितनी", "कितने", "आज", "अभी",
    "शेयर", "शेयरों", "स्टॉक", "प्राइस", "भाव", "दाम", "कीमत", "रेट", "बताओ", "बताइए", "बताएं",
    "लिमिटेड", "लि", "लि.", "कंपनी", "का?", "है?",
}


def has_devanagari(text: str) -> bool:
    return bool(_DEVANAGARI.search(text or ""))


def _abbreviation(word: str) -> str | None:
    """'एचडीएफसी' -> 'HDFC' if the whole word is English letter names (2 or more)."""
    letters, i = [], 0
    while i < len(word):
        for name in _LETTER_NAMES_LONGEST_FIRST:
            if word.startswith(name, i):
                letters.append(_LETTER_NAMES[name])
                i += len(name)
                break
        else:
            return None
    return "".join(letters) if len(letters) >= 2 else None


def _transliterate_word(word: str) -> str:
    out: list[str] = []
    chars = list(word)
    i = 0
    while i < len(chars):
        ch = chars[i]
        nxt = chars[i + 1] if i + 1 < len(chars) else ""
        if ch in _CONSONANTS:
            sound = _CONSONANTS[ch]
            if nxt == _NUKTA and ch in _NUKTA_FORMS:
                sound = _NUKTA_FORMS[ch]
                i += 1
                nxt = chars[i + 1] if i + 1 < len(chars) else ""
            out.append(sound)
            if nxt in _VOWEL_SIGNS:
                out.append(_VOWEL_SIGNS[nxt])
                i += 1
            elif nxt == _VIRAMA:
                i += 1  # half consonant: no vowel
            elif nxt and nxt not in ("ं", "ँ", "ः"):
                out.append("a")  # inherent vowel between consonants
            elif nxt in ("ं", "ँ", "ः"):
                out.append("a")
            # else: end of word -> the inherent 'a' is silent (रिलायंस -> rilayans)
        elif ch in _VOWELS:
            out.append(_VOWELS[ch])
        elif ch in _VOWEL_SIGNS:
            out.append(_VOWEL_SIGNS[ch])
        elif ch == "ं" or ch == "ँ":
            out.append("m" if nxt in _LABIALS else "n")
        elif ch == "ः":
            out.append("h")
        elif ch in _DIGITS:
            out.append(_DIGITS[ch])
        elif ch in (_NUKTA, _VIRAMA, "ऽ", "।", "॥"):
            pass
        else:
            out.append(ch)  # Latin letters, digits, & etc. pass through
        i += 1
    return "".join(out)


def to_latin(text: str) -> str:
    """Hindi company name -> Latin words; abbreviations in capitals, filler words dropped.

    Latin words already in the text are kept as they are ("HDFC बैंक" -> "HDFC baink").
    """
    words = []
    for raw in (text or "").replace("?", " ").replace(",", " ").split():
        if raw in _FILLER:
            continue
        if not has_devanagari(raw):
            words.append(raw)
            continue
        words.append(_abbreviation(raw) or _transliterate_word(raw))
    return " ".join(w for w in words if w)


_FOLDS = [
    ("chh", "C"), ("ch", "C"), ("sh", "s"), ("ph", "f"), ("bh", "b"), ("dh", "d"), ("th", "t"),
    ("kh", "k"), ("gh", "g"), ("jh", "j"), ("ck", "k"), ("q", "k"), ("x", "ks"), ("z", "j"), ("w", "v"),
]


def phonetic_key(word: str) -> str:
    """Sound-alike key: first letter kept (any vowel as '*'), later vowels, y and h
    dropped, similar consonants folded together, doubled letters collapsed.

    "rilayans" / "RELIANCE" -> "rlns";  "infosis" / "INFOSYS" -> "*nfs";
    "airtel" / "eyartel" -> "*rtl";  "vipro" / "WIPRO" -> "vpr".
    """
    w = re.sub(r"[^a-z]", "", (word or "").lower())
    if not w:
        return ""
    for old, new in _FOLDS:
        w = w.replace(old, new)
    w = re.sub(r"c(?=[eiy])", "s", w)  # ce/ci/cy sound like s (RELIANCE, FINANCE)
    w = w.replace("c", "k").replace("C", "c")
    w = re.sub(r"m(?=[pbf])", "n", w)  # nasal before p/b/f: इंफोसिस "imphosis" ~ INFOSYS
    if w.startswith("yu"):
        w = w[1:]  # यूनिलीवर "yunilivar" ~ UNILEVER: a leading "yu" is the letter U
    first = "*" if w[0] in "aeiou" else w[0]
    rest = [c for c in w[1:] if c not in "aeiouyh"]
    key = first + "".join(rest)
    return re.sub(r"(.)\1+", r"\1", key)  # collapse doubles: "tt" -> "t"
