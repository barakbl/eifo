/* Recommendations in the web app: the page of picks, and "More like this".
 *
 * Both come from Eifo's own server - the member's ratings and the catalog's
 * genres, people and years - with no AI anywhere. What each pick says about
 * itself is filled from what the server says it shares, so the reason is the
 * real one rather than a sentence that sounds like one.
 */

import { similarTitles } from "./api.js";

import { DEFAULT_FILTERS, paramsToFilters } from "./format.js";
import { el, replace, titleCard } from "./ui.js";

/** How many titles a row shows. One scroll on a phone, a glance on a desk. */
export const SHELF_SIZE = 12;

/**
 * How the page of picks can be ordered. The empty one is the order they are
 * worked out in - best match first - and the default.
 */
export const PICK_SORTS = ["", "score", "year", "recently_added"];

/**
 * The page's filters from its URL: only what a pick can be narrowed by.
 *
 * `remembered` is the services the catalog is showing - the one selection a
 * viewer makes, wherever they made it - and an address that names its own
 * services wins, as it does there.
 */
export function picksFilters(search, remembered = []) {
  const read = paramsToFilters(search);
  return {
    ...DEFAULT_FILTERS,
    sources: new URLSearchParams(search).has("sources") ? read.sources : [...remembered],
    type: read.type,
    genres: read.genres,
    scoreMin: read.scoreMin,
    runtimeMax: read.runtimeMax,
    sort: PICK_SORTS.includes(read.sort) ? read.sort : "",
  };
}

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
export function shelf({ heading, cards }) {
  return [
    el("div", { class: "shelf__head" }, [
      el("h2", { class: "section__heading shelf__title", text: heading }),
    ]),
    el("ul", { class: "shelf__list" }, cards),
  ];
}

export function cardWithReason(title, language, index, t, reason, actionsFor = null) {
  const card = titleCard(title, language, index, actionsFor ? actionsFor(title.id) : null, t);
  if (reason) card.append(el("p", { class: "shelf__why", text: reason }));
  return card;
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
