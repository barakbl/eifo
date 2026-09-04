/* Account UI: the header menu, and the list controls a title page needs.
 *
 * Both live here because they are the two places a signed-in user acts on
 * their own data, and both have to behave identically when they are pressed
 * faster than the network answers.
 */

import { deleteMyItem, listMyItems, loginUrl, putMyItem } from "./api.js";
import {
  NOTE_MAX_LENGTH,
  RATING_MAX,
  RATING_MIN,
  STARS,
  WANT_TO_WATCH,
  WATCHED,
  fillPercent,
  normalizeRating,
  ratingFromFraction,
  toggleList,
} from "./items.js";
import { el, icon } from "./ui.js";

/** The glyph the catalog already rates with; see the my-list cards. */
const STAR = "\u2605";

/**
 * What each key does to a rating. A number is a step, null clears.
 *
 * Left and right rather than the reading direction's own: a slider's arrows
 * are about the track, and the browser flips them in RTL on its own.
 */
const KEY_STEPS = {
  ArrowRight: 1,
  ArrowUp: 1,
  ArrowLeft: -1,
  ArrowDown: -1,
  Home: RATING_MIN,
  End: RATING_MAX,
  Delete: null,
  Backspace: null,
  "0": null,
};

/** The header's account area: sign-in choices, or the user's own menu. */
export function accountMenu({ user, providers, t, onSignOut }) {
  if (user) return signedInMenu({ user, t, onSignOut });
  if (!providers.length) return null;

  return el(
    "details",
    { class: "account" },
    [
      el("summary", { class: "account__summary", text: t("auth.signIn") }),
      el(
        "div",
        { class: "account__panel" },
        providers.map((provider) =>
          el("a", {
            class: "account__item",
            href: loginUrl(provider),
            text: t("auth.signInWith", { provider: t(`auth.provider.${provider}`) }),
          }),
        ),
      ),
    ],
  );
}

function signedInMenu({ user, t, onSignOut }) {
  return el("details", { class: "account" }, [
    el("summary", { class: "account__summary", "aria-label": t("auth.menu") }, [
      avatar(user),
      el("span", { class: "account__name", text: user.display_name }),
    ]),
    el("div", { class: "account__panel" }, [
      el("a", { class: "account__item", href: "#/me", text: t("mylist.title") }),
      el("a", { class: "account__item", href: "#/settings", text: t("settings.title") }),
      // Only for an administrator, and only as a shortcut: the page checks, and
      // so does every endpoint it calls.
      user.is_admin
        ? el("a", { class: "account__item", href: "#/manage", text: t("manage.title") })
        : null,
      el("button", {
        class: "account__item",
        type: "button",
        text: t("auth.signOut"),
        onClick: onSignOut,
      }),
    ]),
  ]);
}

/** The user's picture, or their initial when the provider sent none. */
export function avatar(user) {
  if (user.avatar_url) {
    return el("img", {
      class: "avatar",
      src: user.avatar_url,
      alt: "",
      referrerpolicy: "no-referrer",
      loading: "lazy",
    });
  }
  return el("span", {
    class: "avatar avatar--initial",
    text: (user.display_name || "?").slice(0, 1),
    "aria-hidden": "true",
  });
}

/**
 * Write a change to one entry, optimistically.
 *
 * The store is updated and the UI repainted before the request goes out, so a
 * press looks done immediately; if the server refuses, the store is put back
 * and the UI repainted again. The viewer never sees a state the server did not
 * agree to, and never waits to find out whether their tap registered.
 *
 * Returns whether it stuck.
 */
export async function commitItem(titleId, items, patch, { repaint, onError } = {}) {
  const rollback = items.apply(titleId, patch);
  repaint?.();

  try {
    if (items.get(titleId) === null) await deleteMyItem(titleId);
    else await putMyItem(titleId, patch);
    return true;
  } catch (error) {
    rollback();
    repaint?.();
    onError?.(error);
    return false;
  }
}

/** Watched / want-to-watch toggles and a 1-10 rating for one title. */
export function titleActions({ titleId, items, t, onError, onChange }) {
  const container = el("div", { class: "actions" });

  function commit(patch) {
    onChange?.();
    return commitItem(titleId, items, patch, { repaint: paint, onError });
  }

  function paint() {
    const entry = items.get(titleId);
    container.replaceChildren(
      statusButton(WANT_TO_WATCH, "item.wantToWatch", "bookmark", entry, t, commit),
      statusButton(WATCHED, "item.watched", "eye", entry, t, commit),
      ratingControl(entry, t, commit),
    );
  }

  paint();
  return container;
}

/**
 * The same two list toggles, sized for a poster in the grid.
 *
 * On the artwork rather than in the card's meta row: at two columns on a phone
 * that row already carries the year and the score, and two more controls would
 * push the whole grid taller for something most cards never use. Over the
 * poster they cost no layout at all, and the state reads across the whole grid
 * at once - which is the point of showing it out here.
 *
 * Always drawn, never on hover: a phone has no hover, and "have I seen this"
 * that only appears when you reach for it is not an answer.
 */
/**
 * Which of the titles just loaded the viewer is already keeping.
 *
 * Asked per page rather than by fetching the whole list up front: somebody with
 * a thousand entries should not download all of them to light up twenty-four
 * cards. A failure here costs the toggles their state, which is worth strictly
 * less than the grid, so it is swallowed.
 *
 * Returns whether anything came back, so a page of titles the viewer has never
 * filed - which is most of them - is not repainted for nothing.
 */
export async function loadMineFor(titleIds, { user, items }) {
  if (!user || !titleIds.length) return false;
  try {
    const mine = await listMyItems({ titleIds }, { pageSize: titleIds.length });
    items.merge(mine.items);
    return mine.items.length > 0;
  } catch {
    // The cards still render; their toggles just start empty.
    return false;
  }
}

export function cardActions({ titleId, items, t, onError }) {
  const container = el("div", { class: "card__actions" });

  function commit(patch) {
    return commitItem(titleId, items, patch, { repaint: paint, onError });
  }

  function paint() {
    const entry = items.get(titleId);
    container.replaceChildren(
      cardToggle(WANT_TO_WATCH, "item.wantToWatch", "bookmark", entry, t, commit),
      cardToggle(WATCHED, "item.watched", "eye", entry, t, commit),
    );
  }

  paint();
  return container;
}

/**
 * One poster toggle: the list's own icon, with on-or-off carried by the fill.
 *
 * The icon does not change when it is on. It names which list this is, and
 * that does not stop being true once the title is on it - the amber says the
 * rest. This is also why it is not a plus turning into a tick: that pair is
 * one button in two states, and two of them side by side read as one control
 * drawn twice rather than as two different lists.
 */
function cardToggle(list, key, iconName, entry, t, commit) {
  const pressed = Boolean(entry?.[list]);
  const label = t(key);

  return el(
    "button",
    {
      class: `card-action${pressed ? " card-action--on" : ""}`,
      type: "button",
      "aria-pressed": String(pressed),
      "aria-label": label,
      title: label,
      onClick: (event) => {
        // The button sits over a link that covers the whole card.
        event.preventDefault();
        event.stopPropagation();
        commit(toggleList(entry, list));
      },
    },
    icon(iconName),
  );
}

/**
 * The private note.
 *
 * Saved on demand rather than as it is typed: a half-finished sentence is not
 * a state worth showing back to anyone, including its author.
 */
export function noteEditor({ titleId, items, t }) {
  const field = el("textarea", {
    class: "note",
    rows: "3",
    maxlength: String(NOTE_MAX_LENGTH),
    placeholder: t("item.notePlaceholder"),
    "aria-label": t("item.note"),
  });
  field.value = items.get(titleId)?.note ?? "";

  const status = el("span", { class: "note__status", role: "status" });

  const save = el("button", {
    class: "button button--quiet",
    type: "button",
    text: t("item.noteSave"),
    onClick: async () => {
      status.textContent = "";
      const saved = await commitItem(titleId, items, { note: field.value.trim() || null });
      field.value = items.get(titleId)?.note ?? "";
      status.textContent = t(saved ? "item.noteSaved" : "item.saveFailed");
    },
  });

  return el("details", { class: "note-editor" }, [
    el("summary", { text: t("item.note") }),
    field,
    el("div", { class: "note-editor__row" }, [save, status]),
  ]);
}

/* The same icon as the poster toggle, beside the words that name it. Seeing
   the two together here is what teaches the icon out in the grid. */
function statusButton(list, key, iconName, entry, t, commit) {
  const pressed = Boolean(entry?.[list]);
  return el(
    "button",
    {
      class: `toggle${pressed ? " toggle--on" : ""}`,
      type: "button",
      "aria-pressed": String(pressed),
      onClick: () => commit(toggleList(entry, list)),
    },
    [icon(iconName), el("span", { text: t(key) })],
  );
}

/**
 * Five stars, clickable by halves, the way every catalogue does it.
 *
 * A slider rather than ten radios or a listbox: the value is one number on a
 * range, which is what a slider is for, and it gets arrow keys, Home and End
 * from the role without any of it being written here. The stored scale is
 * still 1-10 - that is what providers report and what the aggregate is
 * computed in - so aria carries the number out of ten while the eye gets the
 * stars.
 *
 * Two layers, not five glyphs with three states. The filled row is laid over
 * the empty one and clipped to a percentage, so half a star costs a number
 * rather than a second set of characters, and any future quarter would too.
 */
function ratingControl(entry, t, commit) {
  const rating = entry?.rating ?? null;

  const filled = el("span", { class: "stars__on", "aria-hidden": "true", text: STAR.repeat(STARS) });
  const track = el("span", { class: "stars__track" }, [
    el("span", { class: "stars__off", "aria-hidden": "true", text: STAR.repeat(STARS) }),
    filled,
  ]);

  const stars = el("div", {
    class: "stars",
    role: "slider",
    tabindex: "0",
    "aria-label": t("item.rating"),
    "aria-valuemin": "0",
    "aria-valuemax": String(RATING_MAX),
  });

  // What the pointer is promising, kept apart from what is stored: leaving
  // without clicking has to put the real value back, not the last one hovered.
  let preview = null;

  function show(value) {
    const shown = value ?? rating;
    filled.style.setProperty("--fill", `${fillPercent(shown)}%`);
    stars.classList.toggle("stars--empty", shown == null);
    stars.setAttribute("aria-valuenow", String(shown ?? 0));
    stars.setAttribute(
      "aria-valuetext",
      shown == null ? t("item.ratingNone") : t("item.rate", { value: shown }),
    );
    stars.title = shown == null ? t("item.rating") : t("item.rate", { value: shown });
  }

  /** Where along the row a pointer is, reading from the start edge. */
  function fractionAt(event) {
    const box = track.getBoundingClientRect();
    if (!box.width) return 0;
    const from = event.clientX - box.left;
    // Logical, not physical: in Hebrew the first star is the rightmost one.
    const rtl = getComputedStyle(stars).direction === "rtl";
    return (rtl ? box.width - from : from) / box.width;
  }

  track.addEventListener("pointermove", (event) => {
    preview = ratingFromFraction(fractionAt(event));
    show(preview);
  });
  stars.addEventListener("pointerleave", () => {
    preview = null;
    show(null);
  });
  track.addEventListener("click", (event) => {
    const value = ratingFromFraction(fractionAt(event));
    // Pressing the rating it already has takes it off, which is the only way
    // to clear one without a second control standing beside it.
    commit({ rating: value === rating ? null : value });
  });

  stars.addEventListener("keydown", (event) => {
    const step = KEY_STEPS[event.key];
    if (step === undefined) return;
    event.preventDefault();
    const next = typeof step === "number" ? (rating ?? 0) + step : step;
    commit({ rating: next === null || next < RATING_MIN ? null : normalizeRating(next) });
  });

  show(null);
  stars.append(track);
  return stars;
}
