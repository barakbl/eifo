/* The user's own entries, held locally so the UI can answer instantly.
 *
 * A toggle has to look done before the server has heard about it - anything
 * else feels broken on a phone - so every change is applied here first and the
 * caller keeps the undo. When the request fails, the undo runs and the UI is
 * back where it was, which is the only honest thing to show.
 *
 * Deliberately free of DOM and network access so `node --test` can cover it.
 */

export const WATCHED = "watched";
export const WANT_TO_WATCH = "want_to_watch";

export const RATING_MIN = 1;
export const RATING_MAX = 10;
/* Matches the server's validation; the client's copy only shapes the input. */
export const NOTE_MAX_LENGTH = 2000;

/** Whether an entry still says anything worth keeping. */
export function isEmptyEntry(entry) {
  return !entry || (!entry.status && entry.rating == null && !entry.note);
}

/**
 * What pressing a list button means.
 *
 * Pressing the list a title is already in takes it out; pressing the other one
 * moves it. Two buttons, three states, no third button.
 */
export function nextStatus(entry, pressed) {
  return entry?.status === pressed ? null : pressed;
}

/** Clamp a rating to the range the server will accept, or null to clear it. */
export function normalizeRating(value) {
  if (value === null || value === undefined || value === "") return null;
  const rating = Math.round(Number(value));
  if (!Number.isFinite(rating)) return null;
  return Math.min(RATING_MAX, Math.max(RATING_MIN, rating));
}

function merged(previous, patch, titleId) {
  return {
    title_id: titleId,
    status: previous?.status ?? null,
    rating: previous?.rating ?? null,
    note: previous?.note ?? null,
    ...patch,
  };
}

/** A keyed collection of the signed-in user's entries. */
export function createItemStore(entries = []) {
  const byTitle = new Map();

  function replaceAll(next) {
    byTitle.clear();
    for (const entry of next) byTitle.set(Number(entry.title_id), entry);
  }

  replaceAll(entries);

  return {
    get(titleId) {
      return byTitle.get(Number(titleId)) ?? null;
    },

    all() {
      return [...byTitle.values()];
    },

    get size() {
      return byTitle.size;
    },

    replaceAll,

    clear() {
      byTitle.clear();
    },

    /**
     * Apply a change now and return the function that undoes it.
     *
     * The undo restores the exact previous entry rather than reversing the
     * patch, so two changes racing each other cannot leave a half-applied one.
     */
    apply(titleId, patch) {
      const id = Number(titleId);
      const before = byTitle.get(id) ?? null;
      const next = merged(before, patch, id);

      if (isEmptyEntry(next)) byTitle.delete(id);
      else byTitle.set(id, next);

      return () => {
        if (before) byTitle.set(id, before);
        else byTitle.delete(id);
      };
    },
  };
}
