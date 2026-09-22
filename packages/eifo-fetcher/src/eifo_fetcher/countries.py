"""Country names as Israeli catalogues write them, as ISO 3166-1 codes.

Israeli sites name countries in Hebrew prose - "ישראל/צרפת", "ספרד / איטליה",
"צרפת, אוקראינה, הולנד" - and the catalog stores ISO 3166-1 alpha-2 so the
client can render them in whichever language the reader chose.

This began inside the Tel Aviv Cinematheque plugin and moved here when Lev VOD
needed the same table. Two copies would have drifted: each plugin would have
learned the countries its own source happened to list, and a film would have
carried a flag on one service and none on the other.

An unrecognised name is left out rather than guessed. That costs an optional
field and never invents a wrong one - and a name that starts appearing is a
one-line addition here rather than a parser change.
"""

from __future__ import annotations

import re

#: Every country the Israeli sources have listed, checked against the live
#: catalogues of cinema.co.il (2026-08-22) and ticket.lev.co.il (2026-09-22).
#:
#: Several countries appear under more than one spelling because the sites
#: disagree - "ארה\"ב" and "ארהב", "בריטניה" and "אנגליה" - and a viewer typing
#: either means the same place.
HEBREW_COUNTRY_CODES = {
    "ישראל": "IL",
    "צרפת": "FR",
    'ארה"ב': "US",
    "ארהב": "US",
    "ארצות הברית": "US",
    "איטליה": "IT",
    "גרמניה": "DE",
    "בריטניה": "GB",
    "אנגליה": "GB",
    "סקוטלנד": "GB",
    "ספרד": "ES",
    "פורטוגל": "PT",
    "בלגיה": "BE",
    "הולנד": "NL",
    "קנדה": "CA",
    "נורבגיה": "NO",
    "נורווגיה": "NO",
    "שבדיה": "SE",
    "שוודיה": "SE",
    "דנמרק": "DK",
    "פינלנד": "FI",
    "איסלנד": "IS",
    "אירלנד": "IE",
    "אוסטריה": "AT",
    "שוויץ": "CH",
    "שווייץ": "CH",
    "פולין": "PL",
    "צ'כיה": "CZ",
    "סלובקיה": "SK",
    "הונגריה": "HU",
    "רומניה": "RO",
    "בולגריה": "BG",
    "סרביה": "RS",
    "סלובניה": "SI",
    "קרואטיה": "HR",
    "צפון מקדוניה": "MK",
    "בוסניה": "BA",
    "אוקראינה": "UA",
    "רוסיה": "RU",
    "בלארוס": "BY",
    "ליטא": "LT",
    "לטביה": "LV",
    "אסטוניה": "EE",
    "יוון": "GR",  # noqa: RUF001 - Hebrew for Greece, every letter has a Latin lookalike
    "קפריסין": "CY",
    "גיאורגיה": "GE",
    "ארמניה": "AM",
    "לוקסמבורג": "LU",
    "יפן": "JP",
    "סין": "CN",  # noqa: RUF001 - Hebrew for China, every letter has a Latin lookalike
    "הונג קונג": "HK",
    "טייוואן": "TW",
    "טאיוואן": "TW",
    "דרום קוריאה": "KR",
    "קוריאה": "KR",
    "תאילנד": "TH",
    "אינדונזיה": "ID",
    "וייטנאם": "VN",
    "הודו": "IN",
    "איראן": "IR",
    "עיראק": "IQ",
    "טורקיה": "TR",
    "לבנון": "LB",
    "סוריה": "SY",
    "ירדן": "JO",
    "מצרים": "EG",
    "פלסטין": "PS",
    "סעודיה": "SA",
    "קטאר": "QA",
    "מרוקו": "MA",
    "תוניסיה": "TN",
    "אלג'יריה": "DZ",
    "מאוריטניה": "MR",
    "סנגל": "SN",
    "מאלי": "ML",
    "חוף השנהב": "CI",
    "ניגריה": "NG",
    "קניה": "KE",
    "אתיופיה": "ET",
    "דרום אפריקה": "ZA",
    "ברזיל": "BR",
    "ארגנטינה": "AR",
    "צ'ילה": "CL",
    "אורוגוואי": "UY",
    "פרגוואי": "PY",
    "קולומביה": "CO",
    "פרו": "PE",
    "מקסיקו": "MX",
    "קובה": "CU",
    "אוסטרליה": "AU",
    "ניו זילנד": "NZ",
    "בהוטן": "BT",
}

#: The sites separate co-producing countries with a slash, a comma, or the
#: Hebrew conjunction glued to the front of the next word. Only the first two
#: are punctuation a parser can trust, so those are what get split on.
_SEPARATORS = re.compile(r"[/,]")


def country_codes(text: str | None) -> str | None:
    """ "ישראל/צרפת" as "IL,FR", dropping anything not recognised.

    Order is the source's own, and a country named twice is kept once: a
    co-production listed as "צרפת / בלגיה / צרפת" is two countries.
    """
    codes: list[str] = []
    for part in _SEPARATORS.split(text or ""):
        code = HEBREW_COUNTRY_CODES.get(part.strip())
        if code and code not in codes:
            codes.append(code)
    return ",".join(codes) or None
