"""Which column a name belongs in.

The rule these encode is not "Hebrew or English" but "Hebrew, Latin, or neither
- and neither is an answer". Calling a name English because it was not Hebrew is
how "Spirited Away" came to be stored as "千と千尋の神隠し", findable by nobody
who was looking for it.
"""

from __future__ import annotations

import pytest

from eifo_core.naming import is_hebrew, latin_script, split_by_script


class TestIsHebrew:
    @pytest.mark.parametrize("name", ["פאודה", "שטיסל", "סרוגים 2", "Fauda / פאודה"])
    def test_it_finds_hebrew_anywhere_in_the_string(self, name: str) -> None:
        assert is_hebrew(name)

    @pytest.mark.parametrize("name", ["Fauda", "Amélie", "千と千尋の神隠し", "", "2015"])
    def test_everything_else_is_not(self, name: str) -> None:
        assert not is_hebrew(name)


class TestLatinScript:
    @pytest.mark.parametrize(
        "name",
        ["Fargo", "Amélie", "Cien años de soledad", "Se7en", "The Batman (2022)"],
    )
    def test_accents_and_punctuation_are_still_latin(self, name: str) -> None:
        """An English-column name in every sense that matters here."""
        assert latin_script(name)

    @pytest.mark.parametrize("name", ["פאודה", "千と千尋の神隠し", "Дурак", "معجزة"])
    def test_another_script_is_not(self, name: str) -> None:
        assert not latin_script(name)

    @pytest.mark.parametrize("name", ["", "2015", "- / -", "12"])
    def test_a_string_with_no_letters_is_neither(self, name: str) -> None:
        """Nothing to judge, so it is not claimed for the English column."""
        assert not latin_script(name)


class TestSplitByScript:
    def test_it_sorts_each_name_into_its_own_column(self) -> None:
        assert split_by_script("פאודה", "Fauda") == ("פאודה", "Fauda")

    def test_the_order_they_arrive_in_does_not_decide(self) -> None:
        """The script decides, never which field a source happened to use."""
        assert split_by_script("Fauda", "פאודה") == ("פאודה", "Fauda")

    def test_one_name_fills_only_its_own_column(self) -> None:
        assert split_by_script("Fargo", None) == (None, "Fargo")
        assert split_by_script("שטיסל") == ("שטיסל", None)

    def test_a_third_script_is_neither(self) -> None:
        """The case that mattered: Japanese is not English by elimination."""
        assert split_by_script("千と千尋の神隠し", None) == (None, None)

    def test_the_first_of_a_script_wins(self) -> None:
        assert split_by_script("Fauda", "Fauda II") == (None, "Fauda")

    def test_blank_and_missing_names_are_skipped(self) -> None:
        assert split_by_script(None, "", "Fargo") == (None, "Fargo")
