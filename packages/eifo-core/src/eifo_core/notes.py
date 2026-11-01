"""A private note: a little formatted text, kept as HTML nobody has to trust.

The note editor writes HTML, and HTML is a way of running code on somebody's
page. So nothing a client sends is stored as sent. It is read into a small
model - paragraphs and headings, each holding runs of text that may be bold,
italic, underlined, struck through or a link - and written back out from that
model. Markup the model has no word for cannot survive the trip, and it has no
word for a script, an event handler, a style or an image.

The browser does the same again before it shows a note (``web/js/note.js``),
so a row that reached the database some other way is no more able to run than
one that came through here. The two must agree on what a note looks like
written out, and ``web/tests/note-cases.json`` holds the cases both test.

A note with no tags in it is read as plain text, one paragraph per line: that
is every note written before notes had formatting, and what an API client
that has never heard of the formatting will keep sending.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser

#: The blocks a note is made of. Everything else block-shaped - a div, a list
#: item, a table row - becomes a paragraph, so pasted text keeps its lines.
BLOCKS = ("p", "h1", "h2", "h3", "h4", "h5", "h6")

#: A block's own reading direction. Without one it follows its first letter.
DIRECTIONS = ("rtl", "ltr")

#: Where a link may lead. Not javascript:, not data:, not a relative path that
#: would mean something different on every page the note is shown on.
LINK_SCHEMES = ("http", "https", "mailto")
HREF_MAX_LENGTH = 2048

_INLINE = {
    "b": "bold",
    "strong": "bold",
    "i": "italic",
    "em": "italic",
    "u": "underline",
    "s": "strike",
    "strike": "strike",
    "del": "strike",
    "a": "href",
}

_BLOCKISH = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "dd",
        "details",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "pre",
        "section",
        "summary",
        "table",
        "tr",
        "ul",
    }
)

#: Elements whose content is not text anybody wrote: it goes with them.
_DROPPED = frozenset(
    {
        "audio",
        "canvas",
        "embed",
        "head",
        "iframe",
        "math",
        "noscript",
        "object",
        "script",
        "select",
        "style",
        "svg",
        "template",
        "textarea",
        "title",
        "video",
    }
)

_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
)

_SPACES = re.compile(r"[ \t\n\r\f]+")
#: Something that is unmistakably a tag. A bare "<" is not: "I <3 this" is a
#: plain note.
_TAG = re.compile(r"</?[a-zA-Z][^<>]*>")
_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class Run:
    """Text that looks one way all the way along. ``\\n`` is a line break."""

    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    href: str | None = None

    def same_look(self, other: Run) -> bool:
        return (self.bold, self.italic, self.underline, self.strike, self.href) == (
            other.bold,
            other.italic,
            other.underline,
            other.strike,
            other.href,
        )


@dataclass(slots=True)
class Block:
    """A paragraph or a heading."""

    kind: str = "p"
    direction: str | None = None
    runs: list[Run] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(run.text for run in self.runs)


@dataclass(frozen=True, slots=True)
class Note:
    """A note as it is stored, and how long it is to the person who wrote it."""

    html: str
    #: Characters of text, with a line break counted as one. Markup is not
    #: counted: making a word bold should not cost room to write.
    length: int


def clean(value: str | None) -> Note | None:
    """What to store for a note a client sent, or None if it says nothing."""
    blocks = read(value or "")
    if not any(block.text.strip() for block in blocks):
        return None
    return Note(html=render(blocks), length=len(plain_text(blocks)))


def read(value: str) -> list[Block]:
    """A note, from HTML or - when it has no tags in it - from plain text."""
    if not _TAG.search(value):
        blocks = [Block(runs=[Run(_SPACES.sub(" ", line))]) for line in value.splitlines()]
    else:
        reader = _Reader()
        reader.feed(value)
        reader.close()
        blocks = reader.blocks
    return _tidy(blocks)


def render(blocks: Iterable[Block]) -> str:
    """The one way a note is written out."""
    return "".join(_block_html(block) for block in blocks)


def plain_text(blocks: Iterable[Block]) -> str:
    """The words, one line per block, with nothing to say how they looked."""
    return "\n".join(block.text for block in blocks)


def safe_href(value: str | None) -> str | None:
    """A link's address if it is one a note may hold, else None.

    Control characters go before the scheme is looked at, because a browser
    ignores them too: ``java\\tscript:`` is a script to it.
    """
    href = _CONTROL.sub("", (value or "").strip().replace(" ", "%20"))
    if not href or len(href) > HREF_MAX_LENGTH:
        return None
    scheme = _SCHEME.match(href)
    if scheme is None or scheme.group(1).lower() not in LINK_SCHEMES:
        return None
    return href


def _direction(attrs: list[tuple[str, str | None]]) -> str | None:
    for name, value in attrs:
        if name == "dir" and value and value.strip().lower() in DIRECTIONS:
            return value.strip().lower()
    return None


class _Reader(HTMLParser):
    """HTML into blocks, keeping nothing it was not looking for."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self._block: Block | None = None
        # Open block-level elements: (tag, kind, direction).
        self._containers: list[tuple[str, str, str | None]] = []
        # Open inline elements: (tag, href for a link, else None).
        self._marks: list[tuple[str, str | None]] = []
        self._dropping: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._dropping:
            if tag in _DROPPED and tag not in _VOID:
                self._dropping.append(tag)
            return
        if tag in _DROPPED:
            if tag not in _VOID:
                self._dropping.append(tag)
            return
        if tag == "br":
            self._add("\n")
        elif tag in BLOCKS or tag in _BLOCKISH:
            self._block = None
            if tag in _VOID:
                return
            kind = tag if tag in BLOCKS else self._inherited_kind()
            direction = _direction(attrs) or self._inherited_direction()
            self._containers.append((tag, kind, direction))
            if tag in BLOCKS:
                self._open_block()
        elif tag in _INLINE:
            href = None
            if tag == "a":
                href = safe_href(next((v for k, v in attrs if k == "href"), None))
            self._marks.append((tag, href))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._dropping:
            if tag == self._dropping[-1]:
                self._dropping.pop()
            return
        if tag in BLOCKS or tag in _BLOCKISH:
            if any(open_tag == tag for open_tag, _, _ in self._containers):
                while self._containers:
                    if self._containers.pop()[0] == tag:
                        break
            self._block = None
        elif tag in _INLINE:
            for index in range(len(self._marks) - 1, -1, -1):
                if self._marks[index][0] == tag:
                    del self._marks[index]
                    break

    def handle_data(self, data: str) -> None:
        if self._dropping:
            return
        self._add(_SPACES.sub(" ", data))

    def _inherited_kind(self) -> str:
        for _, kind, _ in reversed(self._containers):
            if kind in BLOCKS:
                return kind
        return "p"

    def _inherited_direction(self) -> str | None:
        for _, _, direction in reversed(self._containers):
            if direction:
                return direction
        return None

    def _open_block(self) -> Block:
        self._block = Block(kind=self._inherited_kind(), direction=self._inherited_direction())
        self.blocks.append(self._block)
        return self._block

    def _add(self, text: str) -> None:
        if not text:
            return
        block = self._block or self._open_block()
        looks = {_INLINE[tag] for tag, _ in self._marks}
        # The innermost link that survived; a dropped one does not unlink text
        # an outer one covers.
        href = next((link for _, link in reversed(self._marks) if link), None)
        block.runs.append(
            Run(
                text,
                bold="bold" in looks,
                italic="italic" in looks,
                underline="underline" in looks,
                strike="strike" in looks,
                href=href,
            )
        )


def _tidy(blocks: list[Block]) -> list[Block]:
    """Collapse spaces the way a page shows them, and drop empty ends."""
    for block in blocks:
        block.runs = _tidy_runs(block.runs)
    while blocks and not blocks[0].text:
        blocks.pop(0)
    while blocks and not blocks[-1].text:
        blocks.pop()
    return blocks


def _tidy_runs(runs: list[Run]) -> list[Run]:
    kept: list[tuple[str, Run]] = []
    for run in runs:
        for char in run.text:
            if char == " " and (not kept or kept[-1][0] in " \n"):
                continue
            if char == "\n" and kept and kept[-1][0] == " ":
                kept.pop()
            kept.append((char, run))
    while kept and kept[-1][0] == " ":
        kept.pop()
    # A browser puts a line break at the end of a block to give it height; it
    # is not a line anybody sees, and rendering adds it back.
    if kept and kept[-1][0] == "\n":
        kept.pop()

    merged: list[Run] = []
    for char, look in kept:
        if merged and merged[-1].same_look(look):
            last = merged[-1]
            merged[-1] = Run(
                last.text + char, last.bold, last.italic, last.underline, last.strike, last.href
            )
        else:
            merged.append(Run(char, look.bold, look.italic, look.underline, look.strike, look.href))
    return merged


def _block_html(block: Block) -> str:
    attrs = f' dir="{block.direction}"' if block.direction else ""
    inner = "".join(_run_html(run) for run in block.runs)
    if not block.runs or block.text.endswith("\n"):
        inner += "<br>"
    return f"<{block.kind}{attrs}>{inner}</{block.kind}>"


def _run_html(run: Run) -> str:
    html = "<br>".join(escape(part, quote=False) for part in run.text.split("\n"))
    for on, tag in ((run.strike, "s"), (run.underline, "u"), (run.italic, "i"), (run.bold, "b")):
        if on:
            html = f"<{tag}>{html}</{tag}>"
    if run.href:
        html = f'<a href="{escape(run.href)}">{html}</a>'
    return html
