/* DOM helpers and shared components.
 *
 * Everything builds nodes and assigns textContent. No template string ever
 * reaches innerHTML: catalog text comes from scraped sites and user-entered
 * search terms, and neither is trusted markup.
 */

import { currentSources, formatScore, formatVotes, scoreBand, sourceColorVar } from "./format.js";
import { displayName } from "./i18n.js";

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

/**
 * The small print under a figure: what it is, and how many voted for it.
 *
 * The name is what tells two figures apart, so it is only worth the room when
 * there are two of them - under a lone score it repeats the mark above it,
 * which is a chip saying "IMDb" twice about one number.
 */
export function scoreCaption(score, { named, language } = {}) {
  return [named ? score.provider_name : "", formatVotes(score.vote_count, language)]
    .filter(Boolean)
    .join(" · ");
}

/**
 * One service's chip: its mark, and every figure it reported.
 *
 * A service rather than a score. Rotten Tomatoes measures critics and the
 * crowd separately and Seret does the same, and as two chips apiece they read
 * as two sites disagreeing - on a page whose whole business is telling raters
 * apart. One chip with two numbers is what both sites show on their own pages.
 *
 * Nothing here knows the name of a provider. The mark, the name, the labels
 * and the order all arrive in the payload, written by the plugin that produced
 * the scores; a provider added tomorrow gets a chip without this file changing.
 */
export function ratingChip(group, language) {
  const scores = group.scores ?? [];
  if (!scores.length) return null;

  // The mark replaces the name rather than joining it: both would say the same
  // thing twice, and the chip is small. Hidden from assistive technology
  // because the link's own label already names the service.
  const mark = group.logo_url
    ? el("img", {
        class: "rating__logo",
        src: group.logo_url,
        alt: "",
        "aria-hidden": "true",
        loading: "lazy",
        decoding: "async",
      })
    : el("span", { class: "rating__provider", text: group.name });

  const figures = scores.map((score) => {
    const caption = scoreCaption(score, { named: scores.length > 1, language });
    return el("span", { class: "rating__score" }, [
      el("span", { class: "rating__value", text: score.score_display }),
      caption ? el("span", { class: "rating__caption", text: caption }) : null,
    ]);
  });

  const children = [mark, el("span", { class: "rating__scores" }, figures)];
  const label = `${group.name}: ${scores
    .map((score) => `${score.provider_name} ${score.score_display}`)
    .join(", ")}`;

  if (!group.url) {
    return el("li", {}, el("span", { class: "rating", "aria-label": label }, children));
  }

  return el(
    "li",
    {},
    el(
      "a",
      {
        class: "rating",
        href: group.url,
        rel: "noopener noreferrer",
        target: "_blank",
        "aria-label": label,
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

const SVG_NS = "http://www.w3.org/2000/svg";

/**
 * The line art for the two lists, on a 24-unit grid.
 *
 * A bookmark for the one you mean to get to and an eye for the one you already
 * have is what the services that track both concepts settled on - Letterboxd
 * and JustWatch both draw it this way, and IMDb's watchlist is a bookmark.
 * Each says which list it is; a tick or a plus only says on or off, which is a
 * thing to know about a control rather than a name for one.
 */
const ICONS = {
  // The ribbon with its notch cut out of the bottom edge.
  bookmark: ["M6 3.5h12v17l-6-3.6-6 3.6z"],
  // Lid, then pupil. Drawn as two strokes so it reads at 16px.
  eye: [
    "M2 12s3.8-6.5 10-6.5S22 12 22 12s-3.8 6.5-10 6.5S2 12 2 12z",
    "M12 9.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5z",
  ],
};

/**
 * An inline icon, built node by node.
 *
 * createElementNS rather than markup in a string: this file never hands
 * anything to innerHTML, and an SVG is not the place to make the first
 * exception.
 */
export function icon(name, { title = "" } = {}) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("class", "icon");
  // The button beside it carries the name; a second copy would read it twice.
  svg.setAttribute("aria-hidden", "true");
  if (title) svg.setAttribute("title", title);

  for (const d of ICONS[name] ?? []) {
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}

/** Placeholder cards, so a loading grid has the shape of the real one. */
export function skeletonCards(count = 12) {
  return Array.from({ length: count }, () =>
    el("li", {}, el("div", { class: "card" }, el("div", { class: "card__poster skeleton" }))),
  );
}


/* How many posters load eagerly: roughly one screenful, so the grid paints
 * without racing to fetch everything below the fold. */
const EAGER_IMAGES = 12;

/**
 * One title in a grid: poster, name, year and score.
 *
 * `actions` is an optional node laid over the poster's corner. It is a sibling
 * of the card rather than a child: the card is one big anchor, and a button
 * inside an anchor is neither valid nor clickable - the link swallows it.
 */
export function titleCard(title, language, index = 0, actions = null) {
  const name = displayName(title, language);
  const eager = index < EAGER_IMAGES;
  const poster = title.poster_url
    ? el("img", {
        src: title.poster_url,
        alt: "",
        decoding: "async",
        loading: eager ? "eager" : "lazy",
        fetchpriority: eager ? "high" : "low",
      })
    : el("div", { class: "card__placeholder", text: name.slice(0, 1), "aria-hidden": "true" });

  return el("li", { class: actions ? "card-slot" : "" }, [
    el("a", { class: "card", href: `#/title/${title.id}` }, [
      el("div", { class: "card__poster" }, [poster, spine(currentSources(title.availability))]),
      el("span", { class: "card__title", text: name }),
      el("span", { class: "card__meta" }, [
        title.year ? el("span", { text: String(title.year) }) : null,
        scorePill(title.score),
      ]),
    ]),
    actions,
  ]);
}
