/* A private note: paragraphs and headings of text that may be bold, italic,
 * underlined, struck through or a link, and nothing else.
 *
 * The editor is a contenteditable, and what a browser leaves in one is
 * whatever it likes: spans, styles, pasted images, other sites' markup. None of
 * that is saved or shown. A note is read out of the editor into blocks of runs,
 * and written back out from those, so the only markup that can come out is the
 * markup this file writes. The server does the same again with its own copy
 * of these rules (eifo_core/notes.py), and web/tests/note-cases.json holds the
 * cases the two must agree on.
 *
 * Free of the DOM: it reads anything shaped like a node, so `node --test` can
 * hand it a tree built by hand.
 */

export const NOTE_BLOCKS = ["p", "h1", "h2", "h3", "h4", "h5", "h6"];
export const DIRECTIONS = ["rtl", "ltr"];
export const HREF_MAX_LENGTH = 2048;

const LINK_SCHEMES = ["http", "https", "mailto"];

const INLINE = {
  b: "bold",
  strong: "bold",
  i: "italic",
  em: "italic",
  u: "underline",
  s: "strike",
  strike: "strike",
  del: "strike",
};

const BLOCKISH = new Set([
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
]);

/* Elements whose content is not text anybody wrote: it goes with them. */
const DROPPED = new Set([
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
]);

const ELEMENT_NODE = 1;
const TEXT_NODE = 3;
const SPACES = /[ \t\n\r\f]+/g;
const SCHEME = /^([a-zA-Z][a-zA-Z0-9+.-]*):/;
const CONTROL = /[\x00-\x1f\x7f]/g;
const PLAIN = Object.freeze({ bold: false, italic: false, underline: false, strike: false, href: null });

/**
 * A link's address if a note may hold it, else null.
 *
 * Control characters go before the scheme is looked at, because a browser
 * ignores them too: "java\tscript:" is a script to it.
 */
export function safeHref(value) {
  const href = String(value ?? "")
    .trim()
    .replaceAll(" ", "%20")
    .replace(CONTROL, "");
  if (!href || href.length > HREF_MAX_LENGTH) return null;
  const scheme = SCHEME.exec(href);
  if (!scheme || !LINK_SCHEMES.includes(scheme[1].toLowerCase())) return null;
  return href;
}

/**
 * What somebody typed into the link box, as an address, or null.
 *
 * Nobody types "https://" in front of imdb.com, and an address with an @ and no
 * slash is somebody to write to.
 */
export function linkFromInput(value) {
  const typed = String(value ?? "").trim();
  if (!typed) return null;
  if (SCHEME.test(typed) && !/^[^:/]+:\d/.test(typed)) return safeHref(typed);
  if (/^[^\s@/]+@[^\s@/]+\.[^\s@/]+$/.test(typed)) return safeHref(`mailto:${typed}`);
  return safeHref(`https://${typed.replace(/^\/+/, "")}`);
}

/** Blocks, read out of a node and everything inside it. */
export function readNote(root) {
  const blocks = [];
  let current = null;

  const open = (kind, dir) => {
    current = { kind, dir, runs: [] };
    blocks.push(current);
    return current;
  };

  const add = (text, look, context) => {
    if (!text) return;
    (current ?? open(context.kind, context.dir)).runs.push({ text, ...look });
  };

  const walk = (node, context, look) => {
    for (const child of node.childNodes ?? []) {
      if (child.nodeType === TEXT_NODE) add(child.nodeValue.replace(SPACES, " "), look, context);
      else if (child.nodeType === ELEMENT_NODE) element(child, context, look);
    }
  };

  const element = (node, context, look) => {
    const tag = node.nodeName.toLowerCase();
    if (DROPPED.has(tag)) return;
    if (tag === "br") {
      add("\n", look, context);
      return;
    }

    if (NOTE_BLOCKS.includes(tag) || BLOCKISH.has(tag)) {
      current = null;
      const inner = {
        kind: NOTE_BLOCKS.includes(tag) ? tag : context.kind,
        dir: directionOf(node) ?? context.dir,
      };
      if (NOTE_BLOCKS.includes(tag)) open(inner.kind, inner.dir);
      walk(node, inner, styled(node, look));
      current = null;
      return;
    }

    let next = look;
    if (tag === "a") {
      next = { ...look, href: safeHref(node.getAttribute("href")) ?? look.href };
    } else if (INLINE[tag]) {
      next = { ...look, [INLINE[tag]]: true };
    }
    walk(node, context, styled(node, next));
  };

  walk(root, { kind: "p", dir: null }, PLAIN);
  return tidy(blocks);
}

/** The one way a note is written out. */
export function writeNote(blocks) {
  return blocks.map(writeBlock).join("");
}

/** A block's runs alone, for pasting into the middle of a paragraph. */
export function writeRuns(runs) {
  return runs.map(writeRun).join("");
}

/** A link to insert where nothing was selected: the address is its own text. */
export function writeLink(href) {
  return writeRun({ ...PLAIN, text: href, href });
}

/** The words, one line per block. */
export function plainText(blocks) {
  return blocks.map(blockText).join("\n");
}

/** How long a note is to the person writing it: characters, markup aside. */
export function noteLength(blocks) {
  return [...plainText(blocks)].length;
}

/** Whether a note says anything at all. */
export function isBlankNote(blocks) {
  return !blocks.some((block) => blockText(block).trim());
}

/**
 * Which way a block reads: its own direction, or its first letter's.
 *
 * The same rule the page follows for a block with no dir (unicode-bidi:
 * plaintext), so a file made from a note reads the way the note did.
 */
export function blockDirection(block, fallback = "ltr") {
  if (block.dir) return block.dir;
  for (const char of blockText(block)) {
    if (/[\p{Script=Hebrew}\p{Script=Arabic}\p{Script=Syriac}\p{Script=Thaana}]/u.test(char)) {
      return "rtl";
    }
    if (/\p{L}/u.test(char)) return "ltr";
  }
  return fallback;
}

/* -- RTF ------------------------------------------------------------------ */

/* Half-points: 12pt text, headings from 24pt down. */
const RTF_SIZES = { title: 36, p: 24, h1: 48, h2: 40, h3: 32, h4: 28, h5: 24, h6: 22 };

/**
 * A note as an RTF document, with the title it is about as its heading.
 *
 * Plain ASCII: everything past it is a \u escape, which is what lets Hebrew
 * and emoji through any reader. A right-to-left paragraph says so (\rtlpar)
 * and is aligned right, and bold and italic are set for both scripts (\b and
 * \ab): Word formats right-to-left text with the second set and ignores the
 * first.
 */
export function noteToRtf(blocks, { title = "", direction = "ltr" } = {}) {
  const lines = [
    "{\\rtf1\\ansi\\ansicpg1252\\deff0\\uc1",
    "{\\fonttbl{\\f0\\fswiss\\fcharset0 Arial;}{\\f1\\fswiss\\fcharset177 Arial;}}",
    "{\\colortbl;\\red5\\green99\\blue193;}",
  ];
  if (title) lines.push(`{\\info{\\title ${rtfText(title)}}}`);
  lines.push("\\viewkind4");

  const heading = title ? [{ kind: "title", dir: null, runs: [{ ...PLAIN, text: title }] }] : [];
  for (const block of [...heading, ...blocks]) lines.push(rtfBlock(block, direction));
  lines.push("}");
  return `${lines.join("\n")}\n`;
}

/** A file name for a title's note: the title, without what a file system refuses. */
export function noteFileName(title, extension = "rtf") {
  const name = String(title ?? "")
    .replace(/[\\/:*?"<>|\x00-\x1f\x7f]/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 100)
    .trim();
  return `${name || "note"}.${extension}`;
}

function rtfBlock(block, fallback) {
  const rtl = blockDirection(block, fallback) === "rtl";
  const size = RTF_SIZES[block.kind] ?? RTF_SIZES.p;
  const heading = block.kind !== "p";
  const spacing = heading ? "\\sb240\\sa120" : "\\sa120";
  const paragraph = `\\pard\\plain${rtl ? "\\rtlpar\\qr" : "\\ltrpar\\ql"}${spacing}`;
  const runs = block.runs.map((run) => rtfRun(run, { size, heading })).join("");
  return `${paragraph}${runs}\\par`;
}

function rtfRun(run, { size, heading }) {
  const rtl = /[\p{Script=Hebrew}\p{Script=Arabic}]/u.test(run.text);
  const words = [rtl ? "\\rtlch\\f1" : "\\ltrch\\f0", `\\fs${size}\\afs${size}`];
  if (run.bold || heading) words.push("\\b\\ab");
  if (run.italic) words.push("\\i\\ai");
  if (run.underline || run.href) words.push("\\ul");
  if (run.strike) words.push("\\strike");
  if (run.href) words.push("\\cf1");
  const group = `{${words.join("")} ${rtfText(run.text)}}`;
  if (!run.href) return group;
  const target = run.href.replaceAll('"', "%22");
  return `{\\field{\\*\\fldinst{HYPERLINK "${rtfText(target)}"}}{\\fldrslt${group}}}`;
}

function rtfText(text) {
  let out = "";
  for (const char of text) {
    const code = char.codePointAt(0);
    if (char === "\\" || char === "{" || char === "}") out += `\\${char}`;
    else if (char === "\n") out += "\\line ";
    else if (char === "\t") out += "\\tab ";
    else if (code === 0xa0) out += "\\~";
    else if (code >= 0x20 && code < 0x7f) out += char;
    else if (code > 0x7f) {
      // One escape per UTF-16 unit: RTF's \u is sixteen bits, and signed.
      for (let i = 0; i < char.length; i += 1) {
        const unit = char.charCodeAt(i);
        out += `\\u${unit > 0x7fff ? unit - 0x10000 : unit}?`;
      }
    }
  }
  return out;
}

/* -- emoji ---------------------------------------------------------------- */

/** What the picker offers, by tab. The first of each is the tab's own face. */
export const EMOJI_GROUPS = [
  {
    key: "faces",
    emoji: [
      "😀", "😃", "😄", "😁", "😆", "😅", "😂", "🤣", "🙂", "😉", "😊", "😇", "🥰", "😍",
      "🤩", "😘", "😋", "😎", "🤓", "🧐", "🤔", "🤨", "😐", "😑", "🙄", "😏", "😬", "😴",
      "🥱", "😮", "😲", "😳", "🥺", "😢", "😭", "😱", "😤", "😡", "🤯", "🤢", "🥳", "😈",
    ],
  },
  {
    key: "hearts",
    emoji: [
      "❤️", "🧡", "💛", "💚", "💙", "💜", "🖤", "🤍", "🤎", "💔", "💕", "💖", "💯", "⭐",
      "🌟", "✨", "🔥", "💥", "💫", "🏆",
    ],
  },
  {
    key: "hands",
    emoji: [
      "👍", "👎", "👏", "🙌", "🙏", "👌", "✌️", "🤞", "🤝", "💪", "👀", "🤷", "🤦", "👋",
    ],
  },
  {
    key: "screen",
    emoji: [
      "🎬", "🎥", "📽️", "🎞️", "📺", "🍿", "🎟️", "🎭", "🎵", "🎶", "📚", "🕵️", "🚀", "👽",
      "🧟", "🧛", "🦸", "🐉", "⚔️", "🔪", "👻", "💀", "🤖", "🦖",
    ],
  },
  {
    key: "things",
    emoji: [
      "✅", "❌", "❓", "❗", "⏳", "📅", "🔁", "⏸️", "▶️", "🛋️", "🌙", "☕", "🍕", "🍷",
      "🎉", "💤", "📝", "📌", "🇮🇱",
    ],
  },
];

/* -- reading and writing -------------------------------------------------- */

function directionOf(node) {
  const value = node.getAttribute?.("dir")?.trim().toLowerCase();
  return DIRECTIONS.includes(value) ? value : null;
}

/*
 * What an inline style says, for text pasted from somewhere that writes bold
 * as a style - Google Docs, or the browser itself after some edits. It even
 * wraps a whole paste in <b style="font-weight:normal">, which would otherwise
 * make all of it bold.
 */
function styled(node, look) {
  const style = node.getAttribute?.("style")?.toLowerCase();
  if (!style) return look;
  const next = { ...look };
  const weight = /font-weight\s*:\s*([a-z0-9]+)/.exec(style)?.[1];
  if (weight) next.bold = weight === "bold" || weight === "bolder" || Number(weight) >= 600;
  const fontStyle = /font-style\s*:\s*([a-z]+)/.exec(style)?.[1];
  if (fontStyle) next.italic = fontStyle === "italic" || fontStyle === "oblique";
  const decoration = /text-decoration(?:-line)?\s*:\s*([^;]+)/.exec(style)?.[1];
  if (decoration?.includes("underline")) next.underline = true;
  if (decoration?.includes("line-through")) next.strike = true;
  return next;
}

function blockText(block) {
  return block.runs.map((run) => run.text).join("");
}

function sameLook(a, b) {
  return (
    a.bold === b.bold &&
    a.italic === b.italic &&
    a.underline === b.underline &&
    a.strike === b.strike &&
    a.href === b.href
  );
}

/* Collapse spaces the way a page shows them, and drop empty ends. */
function tidy(blocks) {
  for (const block of blocks) block.runs = tidyRuns(block.runs);
  while (blocks.length && !blockText(blocks[0])) blocks.shift();
  while (blocks.length && !blockText(blocks.at(-1))) blocks.pop();
  return blocks;
}

function tidyRuns(runs) {
  const kept = [];
  for (const run of runs) {
    for (const char of run.text) {
      const last = kept.at(-1)?.[0];
      if (char === " " && (last === undefined || last === " " || last === "\n")) continue;
      if (char === "\n" && last === " ") kept.pop();
      kept.push([char, run]);
    }
  }
  while (kept.at(-1)?.[0] === " ") kept.pop();
  // A browser puts a line break at the end of a block to give it height; it is
  // not a line anybody sees, and writing the block adds it back.
  if (kept.at(-1)?.[0] === "\n") kept.pop();

  const merged = [];
  for (const [char, look] of kept) {
    const last = merged.at(-1);
    if (last && sameLook(last, look)) last.text += char;
    else
      merged.push({
        text: char,
        bold: Boolean(look.bold),
        italic: Boolean(look.italic),
        underline: Boolean(look.underline),
        strike: Boolean(look.strike),
        href: look.href ?? null,
      });
  }
  return merged;
}

function writeBlock(block) {
  const dir = block.dir ? ` dir="${block.dir}"` : "";
  let inner = block.runs.map(writeRun).join("");
  if (!block.runs.length || blockText(block).endsWith("\n")) inner += "<br>";
  return `<${block.kind}${dir}>${inner}</${block.kind}>`;
}

function writeRun(run) {
  let html = run.text.split("\n").map(escapeText).join("<br>");
  for (const [on, tag] of [
    [run.strike, "s"],
    [run.underline, "u"],
    [run.italic, "i"],
    [run.bold, "b"],
  ]) {
    if (on) html = `<${tag}>${html}</${tag}>`;
  }
  if (run.href) html = `<a href="${escapeAttribute(run.href)}">${html}</a>`;
  return html;
}

/* Exactly what Python's html.escape does, so both sides write the same bytes. */
function escapeText(text) {
  return text.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

function escapeAttribute(text) {
  return escapeText(text).replaceAll('"', "&quot;").replaceAll("'", "&#x27;");
}
