/* The private note's editor: a page of formatted text, saved when asked.
 *
 * A contenteditable driven by execCommand rather than an editor library: the
 * app has no build step, and what a note can hold is small. execCommand is old
 * and every browser still runs it, and it keeps typing, undo and the keyboard
 * shortcuts the browser's own.
 *
 * What the browser leaves in the editor is never what gets saved or shown. A
 * note is read out through note.js into paragraphs and runs, and written back
 * from those, before it leaves this page - and a note arriving from the server
 * is parsed somewhere it cannot run (DOMParser) and rebuilt node by node.
 */

import { commitItem } from "./account.js";
import { directionOf } from "./i18n.js";
import { NOTE_MAX_LENGTH } from "./items.js";
import {
  EMOJI_GROUPS,
  NOTE_BLOCKS,
  isBlankNote,
  linkFromInput,
  noteFileName,
  noteLength,
  noteToRtf,
  readNote,
  safeHref,
  writeLink,
  writeNote,
  writeRuns,
} from "./note.js";
import { el, icon } from "./ui.js";

const BLOCK_SELECTOR = NOTE_BLOCKS.join(",");

/* The marks, as the command that toggles each and the letter on its button. */
const MARKS = [
  { command: "bold", key: "note.bold", letter: "B" },
  { command: "italic", key: "note.italic", letter: "I" },
  { command: "underline", key: "note.underline", letter: "U" },
  { command: "strikeThrough", key: "note.strike", letter: "S" },
];

/* The counter appears near the limit rather than all the time: most notes are
 * a line, and a number under every one of them would be noise. */
const COUNT_FROM = 0.9;

/** A note's HTML as blocks, parsed in a document that cannot run anything. */
export function parseNote(html) {
  if (!html) return [];
  return readNote(new DOMParser().parseFromString(html, "text/html").body);
}

/**
 * The private note.
 *
 * Saved on demand rather than as it is typed: a half-finished sentence is not
 * a state worth showing back to anyone, including its author.
 */
export function noteEditor({ titleId, items, t, titleName = "", language = "he" }) {
  const editable = el("div", {
    class: "note",
    contenteditable: "true",
    role: "textbox",
    "aria-multiline": "true",
    "aria-label": t("item.note"),
    "data-placeholder": t("item.notePlaceholder"),
    spellcheck: "true",
  });

  // Where the caret was. A click on the toolbar - and any tap on a phone -
  // takes the selection away; every command puts it back first.
  let savedRange = null;

  const status = el("span", { class: "note__status", role: "status" });
  const counter = el("span", { class: "note__count" });

  /* -- the selection ----------------------------------------------------- */

  function inEditor(node) {
    return Boolean(node) && (node === editable || editable.contains(node));
  }

  function rememberSelection() {
    const selection = document.getSelection();
    if (selection?.rangeCount && inEditor(selection.anchorNode)) {
      savedRange = selection.getRangeAt(0).cloneRange();
    }
  }

  function restoreSelection() {
    editable.focus({ preventScroll: true });
    const selection = document.getSelection();
    if (!selection) return;
    if (savedRange && inEditor(savedRange.commonAncestorContainer)) {
      selection.removeAllRanges();
      selection.addRange(savedRange);
    } else if (!inEditor(selection.anchorNode)) {
      selection.selectAllChildren(editable);
      selection.collapseToEnd();
    }
  }

  function exec(command, value = null) {
    restoreSelection();
    document.execCommand(command, false, value);
    rememberSelection();
  }

  function closestIn(node, selector) {
    let at = node?.nodeType === Node.ELEMENT_NODE ? node : node?.parentElement;
    while (at && at !== editable) {
      if (at.matches(selector)) return at;
      at = at.parentElement;
    }
    return null;
  }

  function anchorAtCaret() {
    return savedRange ? closestIn(savedRange.startContainer, "a") : null;
  }

  /* The innermost blocks the selection touches. */
  function selectedBlocks() {
    if (!savedRange) return [];
    const touched = [...editable.querySelectorAll(BLOCK_SELECTOR)].filter((block) =>
      savedRange.intersectsNode(block),
    );
    return touched.filter((block) => !touched.some((other) => other !== block && block.contains(other)));
  }

  /* -- the toolbar ------------------------------------------------------- */

  // mousedown is where focus moves; stopping it there keeps the selection in
  // the editor for the click that follows.
  const keepFocus = (event) => event.preventDefault();

  function tool(label, content, onClick, extra = {}) {
    return el(
      "button",
      {
        type: "button",
        class: `note-tool ${extra.class ?? ""}`.trim(),
        title: label,
        "aria-label": label,
        "aria-pressed": extra.pressable ? "false" : undefined,
        "aria-expanded": extra.expandable ? "false" : undefined,
        onMousedown: keepFocus,
        onClick,
      },
      content,
    );
  }

  const blockType = el(
    "select",
    {
      class: "note-tool note-tool--block",
      "aria-label": t("note.blockType"),
      title: t("note.blockType"),
      onChange: () => setBlockType(blockType.value),
    },
    NOTE_BLOCKS.map((tag) =>
      el("option", {
        value: tag,
        text: tag === "p" ? t("note.paragraph") : t("note.heading", { level: tag.slice(1) }),
      }),
    ),
  );

  const markButtons = MARKS.map((mark) => ({
    ...mark,
    button: tool(t(mark.key), el("span", { text: mark.letter, "aria-hidden": "true" }), () => {
      exec(mark.command);
      afterEdit();
    }, { class: `note-tool--${mark.command}`, pressable: true }),
  }));

  const linkButton = tool(t("note.link"), icon("link"), () => toggleLinkRow(), {
    expandable: true,
  });
  const rtlButton = tool(t("note.rtl"), icon("rtl"), () => setDirection("rtl"), {
    pressable: true,
  });
  const ltrButton = tool(t("note.ltr"), icon("ltr"), () => setDirection("ltr"), {
    pressable: true,
  });
  const emojiButton = tool(t("note.emoji"), icon("smile"), () => toggleEmoji(), {
    expandable: true,
  });
  const download = tool(
    t("note.downloadRtf"),
    [icon("download"), el("span", { text: "RTF", "aria-hidden": "true" })],
    () => downloadRtf(),
    { class: "note-tool--file" },
  );

  const separator = () => el("span", { class: "note-tool__separator", "aria-hidden": "true" });

  const toolbar = el(
    "div",
    { class: "note-toolbar", role: "toolbar", "aria-label": t("note.toolbar") },
    [
      blockType,
      separator(),
      ...markButtons.map((mark) => mark.button),
      separator(),
      linkButton,
      rtlButton,
      ltrButton,
      emojiButton,
      download,
    ],
  );

  /* What the buttons say about where the caret is. */
  function paintToolbar() {
    if (!savedRange) return;
    for (const mark of markButtons) {
      let on = false;
      try {
        on = document.queryCommandState(mark.command);
      } catch {
        // A browser that cannot say leaves the button unpressed.
      }
      // A link is drawn underlined, and the browser reports that as underline.
      if (on && mark.command === "underline" && anchorAtCaret()) {
        on = Boolean(closestIn(savedRange.startContainer, "u"));
      }
      mark.button.setAttribute("aria-pressed", String(on));
    }
    const block = closestIn(savedRange.startContainer, BLOCK_SELECTOR);
    blockType.value = block ? block.nodeName.toLowerCase() : "p";
    const dir = block?.getAttribute("dir");
    rtlButton.setAttribute("aria-pressed", String(dir === "rtl"));
    ltrButton.setAttribute("aria-pressed", String(dir === "ltr"));
  }

  function setBlockType(tag) {
    restoreSelection();
    // formatBlock makes new elements and leaves their dir behind; carry it.
    const directions = selectedBlocks().map((block) => block.getAttribute("dir"));
    document.execCommand("formatBlock", false, `<${tag}>`);
    rememberSelection();
    const blocks = selectedBlocks();
    if (blocks.length === directions.length) {
      blocks.forEach((block, index) => {
        if (directions[index]) block.setAttribute("dir", directions[index]);
      });
    }
    afterEdit();
  }

  /* Pressing a direction sets it on the paragraphs under the selection;
   * pressing it again gives them back to their first letter. */
  function setDirection(dir) {
    restoreSelection();
    let blocks = selectedBlocks();
    if (!blocks.length) {
      // Text typed into an empty editor sits in no paragraph until Enter.
      document.execCommand("formatBlock", false, "<p>");
      rememberSelection();
      blocks = selectedBlocks();
    }
    const already = blocks.length && blocks.every((block) => block.getAttribute("dir") === dir);
    for (const block of blocks) {
      if (already) block.removeAttribute("dir");
      else block.setAttribute("dir", dir);
    }
    afterEdit();
  }

  /* -- links -------------------------------------------------------------- */

  const linkInput = el("input", {
    class: "note-link__input",
    type: "url",
    dir: "ltr",
    inputmode: "url",
    autocomplete: "off",
    placeholder: "https://",
    "aria-label": t("note.linkAddress"),
    onKeydown: (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        applyLink();
      } else if (event.key === "Escape") {
        event.preventDefault();
        closeLinkRow();
        restoreSelection();
      }
    },
  });
  const linkProblem = el("span", { class: "note-link__problem", role: "status" });
  const linkOpen = el("a", {
    class: "note-link__open",
    target: "_blank",
    rel: "noopener noreferrer",
    text: t("note.linkOpen"),
    hidden: true,
  });
  const linkRemove = el("button", {
    class: "button button--quiet",
    type: "button",
    text: t("note.linkRemove"),
    hidden: true,
    onClick: () => removeLink(),
  });
  const linkRow = el("div", { class: "note-link", hidden: true }, [
    linkInput,
    el("button", {
      class: "button button--quiet",
      type: "button",
      text: t("note.linkApply"),
      onClick: () => applyLink(),
    }),
    linkRemove,
    linkOpen,
    el("button", {
      class: "button button--quiet",
      type: "button",
      text: t("note.linkCancel"),
      onClick: () => {
        closeLinkRow();
        restoreSelection();
      },
    }),
    linkProblem,
  ]);

  function toggleLinkRow() {
    if (!linkRow.hidden) {
      closeLinkRow();
      restoreSelection();
      return;
    }
    closeEmoji();
    rememberSelection();
    const anchor = anchorAtCaret();
    const href = safeHref(anchor?.getAttribute("href"));
    linkInput.value = href ?? "";
    linkRemove.hidden = !anchor;
    linkOpen.hidden = !href;
    if (href) linkOpen.href = href;
    linkProblem.textContent = "";
    linkRow.hidden = false;
    linkButton.setAttribute("aria-expanded", "true");
    linkInput.focus();
    linkInput.select();
  }

  function closeLinkRow() {
    linkRow.hidden = true;
    linkButton.setAttribute("aria-expanded", "false");
  }

  function selectAnchor(anchor) {
    const range = document.createRange();
    range.selectNodeContents(anchor);
    savedRange = range;
    restoreSelection();
  }

  function applyLink() {
    const href = linkFromInput(linkInput.value);
    if (!href) {
      linkProblem.textContent = t("note.linkInvalid");
      return;
    }
    restoreSelection();
    const anchor = anchorAtCaret();
    if (anchor) selectAnchor(anchor);
    if (document.getSelection()?.isCollapsed) {
      // Nothing selected to link: the address goes in as its own text.
      document.execCommand("insertHTML", false, writeLink(href));
    } else {
      document.execCommand("createLink", false, href);
    }
    rememberSelection();
    closeLinkRow();
    afterEdit();
  }

  function removeLink() {
    restoreSelection();
    const anchor = anchorAtCaret();
    if (anchor) selectAnchor(anchor);
    document.execCommand("unlink");
    rememberSelection();
    closeLinkRow();
    afterEdit();
  }

  /* -- emoji -------------------------------------------------------------- */

  const emojiGrid = el("div", { class: "note-emoji__grid", role: "tabpanel" });
  const emojiTabs = EMOJI_GROUPS.map((group, index) =>
    el("button", {
      type: "button",
      class: "note-emoji__tab",
      role: "tab",
      "aria-selected": String(index === 0),
      "aria-label": t(`note.emoji.${group.key}`),
      title: t(`note.emoji.${group.key}`),
      text: group.emoji[0],
      onMousedown: keepFocus,
      onClick: () => showEmojiGroup(index),
    }),
  );
  const emojiPanel = el(
    "div",
    {
      class: "note-emoji",
      hidden: true,
      onKeydown: (event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          closeEmoji();
          restoreSelection();
        }
      },
    },
    [
      el("div", { class: "note-emoji__tabs", role: "tablist", "aria-label": t("note.emoji") }, emojiTabs),
      emojiGrid,
    ],
  );

  function showEmojiGroup(index) {
    emojiTabs.forEach((tab, at) => tab.setAttribute("aria-selected", String(at === index)));
    emojiGrid.setAttribute("aria-label", t(`note.emoji.${EMOJI_GROUPS[index].key}`));
    emojiGrid.replaceChildren(
      ...EMOJI_GROUPS[index].emoji.map((emoji) =>
        el("button", {
          type: "button",
          class: "note-emoji__pick",
          text: emoji,
          "aria-label": emoji,
          onMousedown: keepFocus,
          onClick: () => {
            exec("insertText", emoji);
            afterEdit();
          },
        }),
      ),
    );
  }

  function toggleEmoji() {
    if (!emojiPanel.hidden) {
      closeEmoji();
      restoreSelection();
      return;
    }
    closeLinkRow();
    rememberSelection();
    if (!emojiGrid.childElementCount) showEmojiGroup(0);
    emojiPanel.hidden = false;
    emojiButton.setAttribute("aria-expanded", "true");
  }

  function closeEmoji() {
    emojiPanel.hidden = true;
    emojiButton.setAttribute("aria-expanded", "false");
  }

  /* -- the file ----------------------------------------------------------- */

  function downloadRtf() {
    const rtf = noteToRtf(readNote(editable), {
      title: titleName,
      direction: directionOf(language),
    });
    const url = URL.createObjectURL(new Blob([rtf], { type: "application/rtf" }));
    const link = el("a", { href: url, download: noteFileName(titleName), hidden: true });
    document.body.append(link);
    link.click();
    link.remove();
    // Not straight away: Safari is still reading it when click() returns.
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  /* -- the page ------------------------------------------------------------ */

  function load(html) {
    const blocks = parseNote(html);
    editable.replaceChildren(...renderBlocks(blocks));
    paintCount(blocks);
  }

  function paintCount(blocks = readNote(editable)) {
    const blank = isBlankNote(blocks);
    const length = blank ? 0 : noteLength(blocks);
    editable.toggleAttribute("data-empty", blank);
    const over = length > NOTE_MAX_LENGTH;
    counter.textContent =
      length >= NOTE_MAX_LENGTH * COUNT_FROM
        ? t("note.count", {
            count: length.toLocaleString(language),
            max: NOTE_MAX_LENGTH.toLocaleString(language),
          })
        : "";
    counter.classList.toggle("note__count--over", over);
    counter.title = over ? t("note.tooLong", { max: NOTE_MAX_LENGTH.toLocaleString(language) }) : "";
    save.disabled = over;
    download.disabled = blank;
  }

  function afterEdit() {
    status.textContent = "";
    paintCount();
    paintToolbar();
  }

  const save = el("button", {
    class: "button button--quiet",
    type: "button",
    text: t("item.noteSave"),
    onClick: async () => {
      status.textContent = "";
      const blocks = readNote(editable);
      const note = isBlankNote(blocks) ? null : writeNote(blocks);
      // A failure leaves what was typed where it is, to try again: a long
      // note is not something to take back from somebody over a bad signal.
      const saved = await commitItem(titleId, items, { note });
      status.textContent = t(saved ? "item.noteSaved" : "item.saveFailed");
    },
  });

  editable.addEventListener("focus", () => {
    // Enter makes a paragraph rather than a div, and bold is <b>, not a style.
    document.execCommand("defaultParagraphSeparator", false, "p");
    document.execCommand("styleWithCSS", false, false);
  });
  editable.addEventListener("input", afterEdit);
  editable.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      toggleLinkRow();
    }
  });
  editable.addEventListener("paste", (event) => {
    const data = event.clipboardData;
    if (!data) return;
    event.preventDefault();
    const html = data.getData("text/html");
    if (!html) {
      document.execCommand("insertText", false, data.getData("text/plain"));
      return;
    }
    const blocks = parseNote(html);
    if (!blocks.length) return;
    // One plain paragraph goes into the line the caret is on, not after it.
    const [first] = blocks;
    const markup =
      blocks.length === 1 && first.kind === "p" && !first.dir ? writeRuns(first.runs) : writeNote(blocks);
    document.execCommand("insertHTML", false, markup);
  });
  editable.addEventListener("drop", (event) => {
    // Text can be dragged in; an image or a file cannot become one.
    if (event.dataTransfer?.files?.length) event.preventDefault();
  });
  editable.addEventListener("click", (event) => {
    // A link in an editor is somewhere to type; with Ctrl or ⌘ it opens.
    const anchor = event.target.closest?.("a");
    if (!anchor || !inEditor(anchor) || !(event.metaKey || event.ctrlKey)) return;
    event.preventDefault();
    const href = safeHref(anchor.getAttribute("href"));
    if (href) window.open(href, "_blank", "noopener,noreferrer");
  });

  const onSelectionChange = () => {
    if (!editable.isConnected) {
      document.removeEventListener("selectionchange", onSelectionChange);
      return;
    }
    const selection = document.getSelection();
    if (selection?.rangeCount && inEditor(selection.anchorNode)) {
      rememberSelection();
      paintToolbar();
    }
  };
  document.addEventListener("selectionchange", onSelectionChange);

  load(items.get(titleId)?.note ?? "");

  const details = el("details", { class: "note-editor" }, [
    el("summary", { text: t("item.note") }),
    el("div", { class: "note-editor__body" }, [toolbar, linkRow, emojiPanel, editable]),
    el("div", { class: "note-editor__row" }, [save, status, counter]),
  ]);
  details.addEventListener("toggle", () => {
    // An empty note was opened to be written in; a full one may only be read.
    if (details.open && editable.hasAttribute("data-empty")) restoreSelection();
  });
  return details;
}

/* Blocks as nodes, built element by element - never from a string. */
function renderBlocks(blocks) {
  return blocks.map((block) => {
    const node = el(block.kind, { dir: block.dir ?? undefined });
    for (const run of block.runs) node.append(runNode(run));
    const text = block.runs.map((run) => run.text).join("");
    if (!text || text.endsWith("\n")) node.append(el("br"));
    return node;
  });
}

function runNode(run) {
  let node = document.createDocumentFragment();
  run.text.split("\n").forEach((part, index) => {
    if (index) node.append(el("br"));
    if (part) node.append(document.createTextNode(part));
  });
  for (const [on, tag] of [
    [run.strike, "s"],
    [run.underline, "u"],
    [run.italic, "i"],
    [run.bold, "b"],
  ]) {
    if (on) node = el(tag, {}, node);
  }
  if (run.href) {
    node = el("a", { href: run.href, title: run.href, target: "_blank", rel: "noopener noreferrer" }, node);
  }
  return node;
}
