"""A note is rewritten from a model on the way in, so markup can only be the kind we write."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from eifo_core import notes

#: Shared with the browser's tests: both sides must write a note the same way.
CASES_FILE = Path(__file__).parents[3] / "web" / "tests" / "note-cases.json"


def _cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = json.loads(CASES_FILE.read_text(encoding="utf-8"))["cases"]
    return cases


class TestTheSharedCases:
    @pytest.mark.parametrize("case", _cases(), ids=lambda case: case["name"])
    def test_writes_what_the_browser_writes(self, case: dict[str, Any]) -> None:
        note = notes.clean(case["in"])

        assert (note.html if note else None) == case["out"]
        assert (note.length if note else 0) == case["length"]

    @pytest.mark.parametrize(
        "case", [case for case in _cases() if case["out"]], ids=lambda case: case["name"]
    )
    def test_reading_what_it_wrote_changes_nothing(self, case: dict[str, Any]) -> None:
        note = notes.clean(case["out"])

        assert note is not None
        assert note.html == case["out"]


class TestHostileMarkup:
    """What the shared cases cannot hold: input a browser would parse its own way."""

    @pytest.mark.parametrize(
        "markup",
        [
            '<a href="java\tscript:alert(1)">x</a>',
            '<a href="java\nscript:alert(1)">x</a>',
            '<a href="&#106;avascript:alert(1)">x</a>',
            '<a href="  javascript:alert(1)">x</a>',
            '<a href="\x01javascript:alert(1)">x</a>',
            '<a href="vbscript:msgbox(1)">x</a>',
            "<a href=javascript:alert(1)>x</a>",
        ],
    )
    def test_no_disguise_gets_a_script_link_through(self, markup: str) -> None:
        note = notes.clean(markup)

        assert note is not None
        assert note.html == "<p>x</p>"

    def test_an_unclosed_script_takes_everything_after_it(self) -> None:
        """Failing closed: text that may be code is not guessed at."""
        note = notes.clean("<p>kept</p><script>alert(1)<p>lost</p>")

        assert note is not None
        assert note.html == "<p>kept</p>"

    def test_a_direction_cannot_smuggle_an_attribute(self) -> None:
        note = notes.clean('<p dir="rtl&quot; onmouseover=&quot;alert(1)">x</p>')

        assert note is not None
        assert note.html == "<p>x</p>"

    def test_comments_and_declarations_are_dropped(self) -> None:
        note = notes.clean("<!DOCTYPE html><!-- <script>x</script> --><![CDATA[y]]><p>z</p>")

        assert note is not None
        assert note.html == "<p>z</p>"

    def test_a_whole_page_is_reduced_to_its_text(self) -> None:
        page = (
            "<html><head><title>T</title><style>b{}</style></head>"
            "<body><h1>Head</h1><table><tr><td>a</td><td>b</td></tr></table></body></html>"
        )

        note = notes.clean(page)

        assert note is not None
        assert note.html == "<h1>Head</h1><p>ab</p>"

    def test_a_tag_inside_text_is_text(self) -> None:
        note = notes.clean("<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>")

        assert note is not None
        assert note.html == "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>"

    def test_a_dropped_inner_link_leaves_the_outer_one(self) -> None:
        note = notes.clean('<a href="https://a.com">x<a href="javascript:1">y</a></a>')

        assert note is not None
        assert note.html == '<p><a href="https://a.com">xy</a></p>'

    def test_a_close_with_no_open_is_ignored(self) -> None:
        note = notes.clean("</b></p></h1>a<b>b</i>c</b>")

        assert note is not None
        assert note.html == "<p>a<b>bc</b></p>"


class TestPlainText:
    def test_one_paragraph_per_line(self) -> None:
        note = notes.clean("first\n\nsecond\r\nthird")

        assert note is not None
        assert note.html == "<p>first</p><p><br></p><p>second</p><p>third</p>"
        assert note.length == len("first\n\nsecond\nthird")

    def test_a_heart_is_not_a_tag(self) -> None:
        note = notes.clean("I <3 this & that\nline two")

        assert note is not None
        assert note.html == "<p>I &lt;3 this &amp; that</p><p>line two</p>"

    def test_an_entity_in_plain_text_is_literal(self) -> None:
        note = notes.clean("Tom &amp; Jerry")

        assert note is not None
        assert note.html == "<p>Tom &amp;amp; Jerry</p>"

    @pytest.mark.parametrize("blank", ["", "   ", "\n\n", None])
    def test_nothing_is_no_note(self, blank: str | None) -> None:
        assert notes.clean(blank) is None


class TestSafeHref:
    @pytest.mark.parametrize(
        ("href", "expected"),
        [
            ("https://x.com/a", "https://x.com/a"),
            ("HTTP://X.COM", "HTTP://X.COM"),
            ("mailto:a@b.c", "mailto:a@b.c"),
            ("  https://x.com/a b  ", "https://x.com/a%20b"),
            ("ftp://x.com", None),
            ("x.com", None),
            ("//x.com", None),
            ("#top", None),
            ("", None),
            (None, None),
            ("https://x.com/" + "a" * notes.HREF_MAX_LENGTH, None),
        ],
    )
    def test_only_web_and_mail_links(self, href: str | None, expected: str | None) -> None:
        assert notes.safe_href(href) == expected


def test_plain_text_is_the_words_a_line_per_block() -> None:
    blocks = notes.read('<h2>Why</h2><p><b>so</b><br>good <a href="https://x.com">x</a></p>')

    assert notes.plain_text(blocks) == "Why\nso\ngood x"
