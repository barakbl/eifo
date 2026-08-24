/* Search-as-you-type under the header box: titles, and the people who made them.
 *
 * People are the reason this exists. Thirty thousand of them are in the
 * catalog and every one was reachable only by already being on a title they
 * worked on - "what else has she been in" was answerable, "find her" was not.
 */

import { suggest as fetchSuggestions } from "./api.js";
import { el, replace } from "./ui.js";

/**
 * Shorter than the grid's debounce, because this is the answer somebody is
 * waiting on mid-word rather than a whole page being rebuilt behind them.
 */
const DEBOUNCE_MS = 150;

/** One letter matches half the catalog; two is where a guess becomes useful. */
const MIN_LENGTH = 2;

/** Long enough for a click to land before focus loss closes the list under it. */
const BLUR_GRACE_MS = 120;

export function createSuggest({ input, router, app }) {
  const list = el("ul", {
    class: "suggest",
    id: "suggest-list",
    role: "listbox",
    hidden: true,
  });

  let options = [];
  let active = -1;
  let inFlight = null;
  let timer = null;

  input.setAttribute("role", "combobox");
  input.setAttribute("aria-expanded", "false");
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-controls", "suggest-list");

  function close() {
    list.hidden = true;
    options = [];
    active = -1;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
  }

  function highlight(next) {
    if (!options.length) return;
    // Wraps, so holding one arrow key cannot strand you at an end.
    active = (next + options.length) % options.length;
    options.forEach((option, index) => {
      const on = index === active;
      option.node.classList.toggle("suggest__option--active", on);
      option.node.setAttribute("aria-selected", String(on));
    });
    input.setAttribute("aria-activedescendant", options[active].node.id);
    options[active].node.scrollIntoView({ block: "nearest" });
  }

  function choose(option) {
    close();
    input.blur();
    router.navigate(option.route, [option.id]);
  }

  function render(payload, language, t) {
    const rows = [];
    options = [];

    const add = (labelKey, items, build) => {
      if (!items.length) return;
      rows.push(el("li", { class: "suggest__group", role: "presentation", text: t(labelKey) }));
      for (const item of items) {
        const option = build(item);
        option.node.id = `suggest-${options.length}`;
        options.push(option);
        rows.push(option.node);
      }
    };

    add("suggest.titles", payload.titles, (title) => titleOption(title, language));
    add("suggest.people", payload.people, (person) => personOption(person, language, t));

    if (!options.length) {
      replace(list, el("li", { class: "suggest__empty", role: "presentation", text: t("suggest.empty") }));
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
      return;
    }

    for (const option of options) {
      // mousedown, not click: the input loses focus first, and a list that has
      // already closed cannot be clicked.
      option.node.addEventListener("mousedown", (event) => {
        event.preventDefault();
        choose(option);
      });
    }

    replace(list, rows);
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    active = -1;
  }

  async function ask(query) {
    const { language, t } = app.get();
    inFlight?.abort();
    inFlight = new AbortController();

    try {
      const payload = await fetchSuggestions(query, { signal: inFlight.signal });
      // Keystrokes outrun round trips: an answer to a question that has since
      // changed would replace a newer one.
      if (payload.query !== input.value.trim()) return;
      render(payload, language, t);
    } catch {
      // A failed suggestion is not worth telling anybody about; the search
      // itself still works.
      close();
    }
  }

  function onInput() {
    window.clearTimeout(timer);
    const query = input.value.trim();
    if (query.length < MIN_LENGTH) {
      inFlight?.abort();
      close();
      return;
    }
    timer = window.setTimeout(() => ask(query), DEBOUNCE_MS);
  }

  function onKeydown(event) {
    if (event.key === "Escape") {
      close();
      return;
    }
    if (list.hidden || !options.length) return;

    if (event.key === "ArrowDown") {
      event.preventDefault();
      highlight(active + 1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      highlight(active - 1);
    } else if (event.key === "Enter" && active >= 0) {
      // Only with something chosen: otherwise Enter belongs to the grid, which
      // is filtering to this same text anyway.
      event.preventDefault();
      choose(options[active]);
    }
  }

  input.addEventListener("input", onInput);
  input.addEventListener("keydown", onKeydown);
  input.addEventListener("blur", () => window.setTimeout(close, BLUR_GRACE_MS));

  return { node: list, close };
}

function titleOption(title, language) {
  const name = pick(title.name_he, title.name_en, language);
  const node = el("li", { class: "suggest__option", role: "option", "aria-selected": "false" }, [
    title.poster_url
      ? el("img", { class: "suggest__poster", src: title.poster_url, alt: "", loading: "lazy" })
      : el("span", { class: "suggest__poster suggest__poster--blank", "aria-hidden": "true" }),
    el("span", { class: "suggest__name", text: name }),
    title.year ? el("span", { class: "suggest__meta", text: String(title.year) }) : null,
  ]);
  return { node, id: title.id, route: "title" };
}

function personOption(person, language, t) {
  const name = pick(person.name_he, person.name_en, language);
  const node = el("li", { class: "suggest__option", role: "option", "aria-selected": "false" }, [
    el("span", { class: "suggest__poster suggest__poster--person", "aria-hidden": "true" }),
    el("span", { class: "suggest__name", text: name }),
    el("span", {
      class: "suggest__meta",
      text: t("suggest.credits", { count: person.credit_count }),
    }),
  ]);
  return { node, id: person.id, route: "people" };
}

/** The reader's language, falling back rather than showing an empty row. */
function pick(hebrew, english, language) {
  return language === "he" ? hebrew || english || "" : english || hebrew || "";
}
