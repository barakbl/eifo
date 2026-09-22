"""The Hebrew country table, shared by the Israeli catalogue plugins.

The cases here are the shapes the two live sources actually serve: checked
against every country cinema.co.il listed on 2026-08-22 and every one
ticket.lev.co.il listed on 2026-09-22 (53 distinct names across 364 films).
"""

from __future__ import annotations

import pytest

from eifo_fetcher.countries import HEBREW_COUNTRY_CODES, country_codes


class TestCountryCodes:
    def test_one_country(self) -> None:
        assert country_codes("ישראל") == "IL"

    @pytest.mark.parametrize(
        ("written", "expected"),
        [
            ("ישראל/צרפת", "IL,FR"),  # the Cinematheque writes it closed up
            ("ספרד / איטליה", "ES,IT"),  # Lev spaces its slashes
            ("צרפת, אוקראינה, הולנד", "FR,UA,NL"),  # and sometimes uses commas
        ],
    )
    def test_a_co_production_in_each_separator_the_sites_use(
        self, written: str, expected: str
    ) -> None:
        assert country_codes(written) == expected

    def test_the_sources_order_is_kept(self) -> None:
        """First-named is first-listed: it is usually the lead producer."""
        assert country_codes("צרפת / ישראל") == "FR,IL"

    def test_a_country_named_twice_is_one_country(self) -> None:
        assert country_codes("צרפת / בלגיה / צרפת") == "FR,BE"

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            ('ארה"ב', "ארהב"),  # with and without the quote mark
            ("שוויץ", "שווייץ"),  # one yud or two
            ("נורבגיה", "נורווגיה"),
            ("בריטניה", "אנגליה"),
        ],
    )
    def test_the_sites_spellings_agree_on_a_code(self, first: str, second: str) -> None:
        """Two sites, two spellings, one place - and one flag on the card."""
        assert country_codes(first) == country_codes(second)

    @pytest.mark.parametrize("junk", ["2012", "גרמנית", "יוגוסלביה"])
    def test_what_is_not_a_country_today_is_left_out(self, junk: str) -> None:
        """All three are real values Lev's records carry: a stray year, a
        language where a country belongs, and a state that no longer exists
        and so has no alpha-2 code. Guessing at any of them would put a wrong
        flag on a film."""
        assert country_codes(junk) is None

    def test_an_unknown_name_does_not_lose_the_ones_beside_it(self) -> None:
        assert country_codes("צרפת / יוגוסלביה / ישראל") == "FR,IL"

    @pytest.mark.parametrize("empty", [None, "", "   ", "/", " , "])
    def test_nothing_in_means_nothing_out(self, empty: str | None) -> None:
        assert country_codes(empty) is None


class TestTheTableItself:
    def test_every_code_is_iso_3166_1_alpha_2(self) -> None:
        assert all(
            len(code) == 2 and code.isascii() and code.isupper()
            for code in HEBREW_COUNTRY_CODES.values()
        )

    def test_no_name_carries_a_separator_it_would_be_split_on(self) -> None:
        """A key holding "/" or "," could never be looked up."""
        assert not [name for name in HEBREW_COUNTRY_CODES if "/" in name or "," in name]

    def test_no_name_is_stored_with_surrounding_space(self) -> None:
        """Lookups strip the value, not the key."""
        assert all(name == name.strip() for name in HEBREW_COUNTRY_CODES)
