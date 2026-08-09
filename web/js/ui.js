/* DOM helpers and shared components.
 *
 * Everything builds nodes and assigns textContent. No template string ever
 * reaches innerHTML: catalog text comes from scraped sites and user-entered
 * search terms, and neither is trusted markup.
 */

import { formatScore, formatVotes, scoreBand, sourceColorVar } from "./format.js";

/**
 * Create an element.
 *
 * `text` is assigned as a text node, so any markup in it stays literal.
 */
export function el(tag, options = {}, children = []) {
  const node = document.createElement(tag);
  const { text, class: className, dataset, style, ...attributes } = options;

  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);

  for (const [key, value] of Object.entries(attributes)) {
    if (value === undefined || value === null || value === false) continue;
    if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else {
      node.setAttribute(key, value === true ? "" : String(value));
    }
  }

  for (const [key, value] of Object.entries(dataset ?? {})) node.dataset[key] = value;
  for (const [key, value] of Object.entries(style ?? {})) node.style.setProperty(key, value);

  for (const child of [children].flat()) {
    if (child) node.append(child);
  }

  return node;
}

/** Replace an element's contents. */
export function replace(parent, ...children) {
  parent.replaceChildren(...children.flat().filter(Boolean));
}

/** A score pill, colour-banded, with the band also stated for screen readers. */
export function scorePill(score, { large = false, label = "" } = {}) {
  const band = scoreBand(score);
  const classes = ["score", `score--${band}`, large ? "score--large" : ""].filter(Boolean);
  return el("span", {
    class: classes.join(" "),
    text: formatScore(score),
    title: label,
    "aria-label": label ? `${label}: ${formatScore(score)}` : undefined,
  });
}

/**
 * The colour spine: one segment per service currently offering the title.
 *
 * Decorative on its own - the same information is in the offer list and the
 * card's accessible label - so it is hidden from assistive technology.
 */
export function spine(sourceKeys) {
  if (!sourceKeys.length) return null;

  return el(
    "div",
    { class: "spine", "aria-hidden": "true" },
    sourceKeys.map((key) =>
      el("span", {
        class: "spine__segment",
        style: { "--source-color": sourceColorVar(key) },
      }),
    ),
  );
}

/** A rating pill linking out to the provider it came from. */
export function ratingPill(rating, language) {
  const votes = formatVotes(rating.vote_count, language);
  const children = [
    el("span", { class: "rating__provider", text: rating.provider_name }),
    el("span", { class: "rating__value", text: rating.score_display }),
  ];
  if (votes) children.push(el("span", { class: "rating__votes", text: votes }));

  if (!rating.url) return el("li", {}, el("span", { class: "rating" }, children));

  return el(
    "li",
    {},
    el(
      "a",
      {
        class: "rating",
        href: rating.url,
        rel: "noopener noreferrer",
        target: "_blank",
      },
      children,
    ),
  );
}

/** A designed empty or error state, never a bare message. */
export function stateBlock({ title, body, actionLabel, onAction }) {
  return el("div", { class: "state", role: "status" }, [
    el("div", { class: "state__mark", "aria-hidden": "true" }),
    el("p", { class: "state__title", text: title }),
    body ? el("p", { class: "state__body", text: body }) : null,
    actionLabel
      ? el("button", { class: "button", type: "button", onClick: onAction, text: actionLabel })
      : null,
  ]);
}

/** Placeholder cards, so a loading grid has the shape of the real one. */
export function skeletonCards(count = 12) {
  return Array.from({ length: count }, () =>
    el("li", {}, el("div", { class: "card" }, el("div", { class: "card__poster skeleton" }))),
  );
}
