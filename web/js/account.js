/* Account UI: the header menu, and the list controls a title page needs.
 *
 * Both live here because they are the two places a signed-in user acts on
 * their own data, and both have to behave identically when they are pressed
 * faster than the network answers.
 */

import { deleteMyItem, loginUrl, putMyItem } from "./api.js";
import {
  NOTE_MAX_LENGTH,
  RATING_MAX,
  RATING_MIN,
  WANT_TO_WATCH,
  WATCHED,
  nextStatus,
  normalizeRating,
} from "./items.js";
import { el } from "./ui.js";

const RATINGS = Array.from({ length: RATING_MAX - RATING_MIN + 1 }, (_, i) => RATING_MIN + i);

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

/** Watched / want-to-watch toggles and a 1–10 rating for one title. */
export function titleActions({ titleId, items, t, onError, onChange }) {
  const container = el("div", { class: "actions" });

  function commit(patch) {
    onChange?.();
    return commitItem(titleId, items, patch, { repaint: paint, onError });
  }

  function paint() {
    const entry = items.get(titleId);
    container.replaceChildren(
      statusButton(WANT_TO_WATCH, "item.wantToWatch", entry, t, commit),
      statusButton(WATCHED, "item.watched", entry, t, commit),
      ratingControl(entry, t, commit),
    );
  }

  paint();
  return container;
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

function statusButton(status, key, entry, t, commit) {
  const pressed = entry?.status === status;
  return el("button", {
    class: `toggle${pressed ? " toggle--on" : ""}`,
    type: "button",
    "aria-pressed": String(pressed),
    text: t(key),
    onClick: () => commit({ status: nextStatus(entry, status) }),
  });
}

function ratingControl(entry, t, commit) {
  const select = el(
    "select",
    {
      class: "control rating-select",
      "aria-label": t("item.rating"),
      onChange: (event) => commit({ rating: normalizeRating(event.currentTarget.value) }),
    },
    [
      el("option", { value: "", text: `${t("item.rating")} —` }),
      ...RATINGS.map((value) =>
        el("option", {
          value: String(value),
          text: String(value),
          "aria-label": t("item.rate", { value }),
        }),
      ),
    ],
  );

  select.value = entry?.rating == null ? "" : String(entry.rating);
  return select;
}
