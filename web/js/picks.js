/* Recommendations in the web app: "For you", and "More like this".
 *
 * Both come from Eifo's own server - the member's ratings and the catalog's
 * genres, people and years - with no AI anywhere. What each pick says about
 * itself is filled from what the server says it shares, so the reason is the
 * real one rather than a sentence that sounds like one.
 */

import { forYou, similarTitles } from "./api.js";

import { el, replace, titleCard } from "./ui.js";

/** How many picks a row shows. One scroll on a phone, a glance on a desk. */
export const SHELF_SIZE = 12;

/** "Because you rated Reservoir Dogs 10" - the favourite a pick came from. */
export function forYouReason(pick, language, t) {
  const seed = pick.seed ?? {};
  const name = language === "he" ? seed.name_he || seed.name : seed.name || seed.name_he;
  return t("forYou.because", { title: name ?? "", rating: seed.rating ?? "" });
}

/**
 * What a similar title shares: the people first, since a shared director is
 * the reason that means most, then the genres.
 */
export function sharedReason(card, language, t) {
  const people = (card.because?.people ?? [])
    .slice(0, 2)
    .map((person) =>
      language === "he" ? person.name_he || person.name_en : person.name_en || person.name_he,
    )
    .filter(Boolean);
  if (people.length) return t("similar.with", { names: people.join(", ") });
  const hebrew = language === "he" && card.because?.genres_he?.length;
  const genres = (hebrew ? card.because.genres_he : card.because?.genres) ?? [];
  return genres.length ? genres.join(" · ") : "";
}

/** A titled, horizontally scrolling row of cards, each with its reason. */
export function shelf({ heading, note, cards }) {
  return [
    el("div", { class: "shelf__head" }, [
      el("h2", { class: "section__heading shelf__title", text: heading }),
      note ? el("p", { class: "shelf__note", text: note }) : null,
    ]),
    el("ul", { class: "shelf__list" }, cards),
  ];
}

function cardWithReason(title, language, index, t, reason, actionsFor = null) {
  const card = titleCard(title, language, index, actionsFor ? actionsFor(title.id) : null, t);
  if (reason) card.append(el("p", { class: "shelf__why", text: reason }));
  return card;
}

/**
 * Fill `node` with the member's "For you" row, or hide it.
 *
 * Hidden rather than explained when there is nothing: a member who has not
 * rated anything highly yet is told how to get picks, once, in the row's
 * place - not shown an empty frame.
 */
export async function fillForYou(
  node,
  { sources = [], language, t, signal, actionsFor = null } = {},
) {
  // The row's frame at once, the first time: picks take a moment to work
  // out, and a row appearing after the catalog pushed the whole grid down
  // under the reader's thumb. A refresh keeps the picks it has until the new
  // ones arrive, rather than flashing back to placeholders.
  if (!node.querySelector(".shelf__list")) {
    replace(node, loadingShelf(t));
    node.hidden = false;
  }
  node.setAttribute("aria-busy", "true");
  let picks;
  try {
    picks = await forYou({ sources, limit: SHELF_SIZE }, { signal });
  } catch (error) {
    if (error?.name !== "AbortError") {
      node.hidden = true;
      node.removeAttribute("aria-busy");
    }
    return;
  }
  node.removeAttribute("aria-busy");
  if (!picks.length) {
    replace(node, el("p", { class: "shelf__empty", text: t("forYou.empty") }));
    node.hidden = false;
    return;
  }
  replace(
    node,
    shelf({
      heading: t("forYou.title"),
      note: t("forYou.note"),
      cards: picks.map((pick, index) =>
        // The watch-later toggle on each: saving a pick for the weekend is
        // the most likely thing to do with one.
        cardWithReason(pick, language, index, t, forYouReason(pick, language, t), actionsFor),
      ),
    }),
  );
  node.hidden = false;
}

/** Placeholders shaped like the row's cards: poster, name, reason. */
function loadingShelf(t) {
  // Built like a real card - poster, name, score line, two lines of reason,
  // inside the same .card box so its gaps are the same gaps - so the row is
  // its final height before a single pick has arrived.
  const ghost = () =>
    el("li", { "aria-hidden": "true" }, [
      el("div", { class: "card" }, [
        el("div", { class: "card__poster skeleton" }),
        el("div", { class: "shelf__ghost shelf__ghost--name skeleton" }),
        el("div", { class: "shelf__ghost shelf__ghost--meta skeleton" }),
      ]),
      el("div", { class: "shelf__ghost shelf__ghost--why skeleton" }),
    ]);
  return shelf({
    heading: t("forYou.title"),
    note: t("forYou.note"),
    // As many as there will be picks: fewer fitted on a wide screen without
    // a scrollbar, and the scrollbar arriving with the picks moved the page.
    cards: Array.from({ length: SHELF_SIZE }, ghost),
  });
}

/** Fill `node` with titles like this one, or leave it hidden. */
export async function fillMoreLikeThis(node, { titleId, signedIn, language, t }) {
  let page;
  try {
    page = await similarTitles(titleId, {
      exclude: signedIn ? "listed" : null,
      pageSize: SHELF_SIZE,
    });
  } catch {
    return;
  }
  if (!page.items.length) return;
  replace(
    node,
    shelf({
      heading: t("similar.title"),
      cards: page.items.map((card, index) =>
        cardWithReason(card, language, index, t, sharedReason(card, language, t)),
      ),
    }),
  );
  node.hidden = false;
}
