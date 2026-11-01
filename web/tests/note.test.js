import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";

import {
  EMOJI_GROUPS,
  blockDirection,
  isBlankNote,
  linkFromInput,
  noteFileName,
  noteLength,
  noteToRtf,
  plainText,
  readNote,
  safeHref,
  writeLink,
  writeNote,
  writeRuns,
} from "../js/note.js";

/* The browser reads a note out of a DOM. There is none under `node --test`, so
 * this builds the same shape from well-formed HTML - which is all the shared
 * cases hold. What a real parser does with broken markup is the browser's
 * business, and what the server does with it is tested on the server. */

const VOID = new Set(["br", "hr", "img", "input", "meta", "link", "wbr"]);
const ENTITIES = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " " };

function decode(text) {
  return text.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (match, name) => {
    if (name[0] === "#") {
      const code = name[1] === "x" || name[1] === "X" ? parseInt(name.slice(2), 16) : Number(name.slice(1));
      return String.fromCodePoint(code);
    }
    return ENTITIES[name.toLowerCase()] ?? match;
  });
}

function element(name, attributes = {}) {
  return {
    nodeType: 1,
    nodeName: name.toUpperCase(),
    childNodes: [],
    getAttribute: (key) => (Object.hasOwn(attributes, key) ? attributes[key] : null),
  };
}

function tree(html) {
  const root = element("body");
  const stack = [root];
  const token = /<!--[\s\S]*?-->|<(\/?)([a-zA-Z][a-zA-Z0-9]*)((?:\s+[^\s=>/]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*\/?>|[^<]+/g;
  for (const [whole, closing, name, rawAttributes] of html.matchAll(token)) {
    if (whole.startsWith("<!--")) continue;
    if (!name) {
      stack.at(-1).childNodes.push({ nodeType: 3, nodeValue: decode(whole) });
      continue;
    }
    const tag = name.toLowerCase();
    if (closing) {
      const at = stack.findLastIndex((node) => node.nodeName === tag.toUpperCase());
      if (at > 0) stack.length = at;
      continue;
    }
    const attributes = {};
    for (const [, key, , double, single, bare] of (rawAttributes ?? "").matchAll(
      /([^\s=>/]+)(\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g,
    )) {
      attributes[key.toLowerCase()] = decode(double ?? single ?? bare ?? "");
    }
    const node = element(tag, attributes);
    stack.at(-1).childNodes.push(node);
    if (!VOID.has(tag)) stack.push(node);
  }
  return root;
}

function clean(html) {
  const blocks = readNote(tree(html));
  return isBlankNote(blocks) ? null : writeNote(blocks);
}

const { cases } = JSON.parse(readFileSync(new URL("./note-cases.json", import.meta.url), "utf8"));

describe("a note is written the way the server writes it", () => {
  for (const testCase of cases) {
    it(testCase.name, () => {
      const blocks = readNote(tree(testCase.in));

      assert.equal(isBlankNote(blocks) ? null : writeNote(blocks), testCase.out);
      assert.equal(isBlankNote(blocks) ? 0 : noteLength(blocks), testCase.length);
    });
  }

  for (const testCase of cases.filter((each) => each.out)) {
    it(`reading back "${testCase.name}" changes nothing`, () => {
      assert.equal(clean(testCase.out), testCase.out);
    });
  }
});

describe("what a paste brings with it", () => {
  it("bold written as a style is bold", () => {
    assert.equal(clean('<p><span style="font-weight: 700">x</span></p>'), "<p><b>x</b></p>");
  });

  it("a Google Docs wrapper that says normal weight is not bold", () => {
    const pasted =
      '<b style="font-weight:normal;" id="docs-internal-guid-1"><p>plain ' +
      '<span style="font-style:italic;text-decoration:underline">it</span></p></b>';

    assert.equal(clean(pasted), "<p>plain <i><u>it</u></i></p>");
  });

  it("strikethrough written as a style is struck through", () => {
    assert.equal(
      clean('<p><span style="text-decoration-line: line-through">x</span></p>'),
      "<p><s>x</s></p>",
    );
  });

  it("colours, fonts and sizes are not kept", () => {
    assert.equal(
      clean('<p><span style="color: red; font-size: 40px; font-family: Comic Sans">x</span></p>'),
      "<p>x</p>",
    );
  });
});

describe("safeHref", () => {
  const cases = [
    ["https://x.com/a", "https://x.com/a"],
    ["mailto:a@b.c", "mailto:a@b.c"],
    ["  https://x.com/a b  ", "https://x.com/a%20b"],
    ["java\tscript:alert(1)", null],
    ["\u0001javascript:alert(1)", null],
    ["JAVASCRIPT:alert(1)", null],
    ["data:text/html,x", null],
    ["ftp://x.com", null],
    ["/relative", null],
    ["", null],
    [null, null],
    [`https://x.com/${"a".repeat(2048)}`, null],
  ];
  for (const [href, expected] of cases) {
    it(`${JSON.stringify(href)} is ${JSON.stringify(expected)}`, () => {
      assert.equal(safeHref(href), expected);
    });
  }
});

describe("linkFromInput", () => {
  it("adds https to an address typed without it", () => {
    assert.equal(linkFromInput("imdb.com/title/tt1"), "https://imdb.com/title/tt1");
  });

  it("keeps a port rather than taking the host for a scheme", () => {
    assert.equal(linkFromInput("localhost:8000/x"), "https://localhost:8000/x");
  });

  it("makes an email address a mailto link", () => {
    assert.equal(linkFromInput("me@example.com"), "mailto:me@example.com");
  });

  it("keeps an address that already has its scheme", () => {
    assert.equal(linkFromInput(" http://x.com "), "http://x.com");
  });

  it("refuses a script, however it is typed", () => {
    assert.equal(linkFromInput("javascript:alert(1)"), null);
  });

  it("says nothing for nothing", () => {
    assert.equal(linkFromInput("   "), null);
  });
});

describe("writing", () => {
  it("a link inserted on its own shows its address", () => {
    assert.equal(writeLink("https://x.com/?a=1&b=2"), '<a href="https://x.com/?a=1&amp;b=2">https://x.com/?a=1&amp;b=2</a>');
  });

  it("runs alone have no paragraph around them", () => {
    const [block] = readNote(tree("<p>a <b>b</b></p>"));

    assert.equal(writeRuns(block.runs), "a <b>b</b>");
  });

  it("plain text is the words, a line per block", () => {
    const blocks = readNote(tree('<h2>Why</h2><p><b>so</b><br>good <a href="https://x.com">x</a></p>'));

    assert.equal(plainText(blocks), "Why\nso\ngood x");
  });
});

describe("blockDirection", () => {
  const block = (text, dir = null) => ({ kind: "p", dir, runs: [{ text }] });

  it("follows the first letter", () => {
    assert.equal(blockDirection(block("123 שלום world")), "rtl");
    assert.equal(blockDirection(block("- hello עולם")), "ltr");
  });

  it("is the block's own when it has one", () => {
    assert.equal(blockDirection(block("hello", "rtl")), "rtl");
  });

  it("falls back to the page's when there is no letter", () => {
    assert.equal(blockDirection(block("123 😀"), "rtl"), "rtl");
  });
});

describe("noteToRtf", () => {
  const rtf = (html, options) => noteToRtf(readNote(tree(html)), options);

  it("is one RTF document, in ASCII alone", () => {
    const out = rtf("<p>שלום 😀</p>", { title: "פאודה" });

    assert.match(out, /^\{\\rtf1\\ansi/);
    assert.ok(out.trimEnd().endsWith("}"));
    assert.ok([...out].every((char) => char.charCodeAt(0) < 0x80), "every character is ASCII");
  });

  it("writes Hebrew and emoji as unicode escapes", () => {
    const out = rtf("<p>א😀</p>");

    // א is U+05D0; 😀 is the surrogate pair D83D DE00, signed.
    assert.ok(out.includes("\\u1488?\\u-10179?\\u-8704?"));
  });

  it("puts the title first, as a heading", () => {
    const out = rtf("<p>body</p>", { title: "Fauda" });

    assert.ok(out.indexOf("Fauda") < out.indexOf("body"));
    assert.ok(out.includes("{\\info{\\title Fauda}}"));
  });

  it("makes a right-to-left paragraph right-to-left", () => {
    const out = rtf('<p>שלום</p><p dir="ltr">hello</p>');

    assert.match(out, /\\rtlpar\\qr[^\n]*\\rtlch/);
    assert.match(out, /\\ltrpar\\ql[^\n]*hello/);
  });

  it("keeps each mark, for both scripts", () => {
    const out = rtf("<p><b>b</b><i>i</i><u>u</u><s>s</s></p>");

    assert.ok(out.includes("\\b\\ab b}"));
    assert.ok(out.includes("\\i\\ai i}"));
    assert.ok(out.includes("\\ul u}"));
    assert.ok(out.includes("\\strike s}"));
  });

  it("sizes headings by level", () => {
    const out = rtf("<h1>big</h1><h6>small</h6>");

    assert.ok(out.includes("\\fs48\\afs48\\b\\ab big"));
    assert.ok(out.includes("\\fs22\\afs22\\b\\ab small"));
  });

  it("makes a link a field a word processor can follow", () => {
    const out = rtf('<p><a href="https://x.com/a?b=&quot;c&quot;">x</a></p>');

    assert.ok(out.includes('{\\field{\\*\\fldinst{HYPERLINK "https://x.com/a?b=%22c%22"}}{\\fldrslt{'));
  });

  it("escapes what RTF itself would read as commands", () => {
    const out = rtf("<p>a\\b {c}</p>");

    assert.ok(out.includes("a\\\\b \\{c\\}"));
  });

  it("keeps line breaks and empty lines", () => {
    const out = rtf("<p>a<br>b</p><p><br></p><p>c</p>");

    assert.ok(out.includes("a\\line b"));
    assert.equal(out.match(/\\par\b/g).length, 3);
  });
});

describe("noteFileName", () => {
  it("is the title, without what a file system refuses", () => {
    assert.equal(noteFileName('AC/DC: "Live"?'), "ACDC Live.rtf");
  });

  it("keeps Hebrew", () => {
    assert.equal(noteFileName("פאודה"), "פאודה.rtf");
  });

  it("is 'note' when there is no title", () => {
    assert.equal(noteFileName("  "), "note.rtf");
  });
});

describe("the emoji picker", () => {
  it("offers about a hundred, none twice", () => {
    const all = EMOJI_GROUPS.flatMap((group) => group.emoji);

    assert.ok(all.length >= 90 && all.length <= 130, `${all.length} emoji`);
    assert.equal(new Set(all).size, all.length);
  });

  it("has a name for every tab in both languages", async () => {
    const { translate } = await import("../js/i18n.js");
    for (const group of EMOJI_GROUPS) {
      for (const language of ["he", "en"]) {
        const key = `note.emoji.${group.key}`;
        assert.notEqual(translate(language, key), key, `${language} ${key}`);
      }
    }
  });
});
