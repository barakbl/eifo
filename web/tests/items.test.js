import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  RATING_MAX,
  RATING_MIN,
  STARS,
  WANT_TO_WATCH,
  WATCHED,
  createItemStore,
  fillPercent,
  isEmptyEntry,
  normalizeRating,
  ratingFromFraction,
  ratingInStars,
  toggleList,
} from "../js/items.js";
import { myItemsQuery } from "../js/api.js";

const FAUDA = 1;

describe("toggleList", () => {
  it("files a title under the list that was pressed", () => {
    assert.deepEqual(toggleList(null, WANT_TO_WATCH), { [WANT_TO_WATCH]: true });
  });

  it("takes it out when the list it is already in is pressed again", () => {
    assert.deepEqual(toggleList({ [WATCHED]: true }, WATCHED), { [WATCHED]: false });
  });

  it("leaves the other list alone, because they are not opposites", () => {
    /* Seen it, and would watch it again. The patch does not mention the list
     * that was not pressed, so nothing can quietly clear it. */
    assert.deepEqual(toggleList({ [WANT_TO_WATCH]: true }, WATCHED), { [WATCHED]: true });
  });

  it("can put one title on both lists", () => {
    const entry = { [WANT_TO_WATCH]: true, [WATCHED]: true };
    assert.equal(entry[WANT_TO_WATCH] && entry[WATCHED], true);
    assert.deepEqual(toggleList(entry, WATCHED), { [WATCHED]: false });
  });
});

describe("five stars over a ten point scale", () => {
  /* The wire scale stays 1-10: that is what providers report and what the
   * aggregate is computed in. Half a star is one point, which is the whole
   * reason the halves are worth having. */

  it("reads four and a half stars as nine", () => {
    assert.equal(ratingFromFraction(0.9), 9);
    assert.equal(ratingInStars(9), 4.5);
  });

  it("fills nine tenths of the row for a nine", () => {
    assert.equal(fillPercent(9), 90);
  });

  it("gives the left half of a star to that star's half", () => {
    /* Landing anywhere in the first star's left half means half a star, and
     * its right half means the whole one - the way every site does it. */
    assert.equal(ratingFromFraction(0.05), 1);
    assert.equal(ratingFromFraction(0.15), 2);
  });

  it("cannot be dragged below half a star", () => {
    /* Clearing is its own gesture, not the far end of this one. */
    assert.equal(ratingFromFraction(0), RATING_MIN);
    assert.equal(ratingFromFraction(-1), RATING_MIN);
  });

  it("stops at five stars", () => {
    assert.equal(ratingFromFraction(1), RATING_MAX);
    assert.equal(ratingFromFraction(2), RATING_MAX);
    assert.equal(ratingInStars(RATING_MAX), STARS);
  });

  it("shows nothing for a title nobody has rated", () => {
    assert.equal(fillPercent(null), 0);
    assert.equal(ratingInStars(null), null);
  });
});

describe("normalizeRating", () => {
  it("keeps a rating in range", () => {
    assert.equal(normalizeRating("7"), 7);
  });

  it("clamps rather than rejecting, so a stray value never reaches the server", () => {
    assert.equal(normalizeRating(0), 1);
    assert.equal(normalizeRating(99), 10);
  });

  it("treats an empty selection as clearing the rating", () => {
    assert.equal(normalizeRating(""), null);
    assert.equal(normalizeRating(null), null);
    assert.equal(normalizeRating(undefined), null);
  });

  it("refuses to invent a number", () => {
    assert.equal(normalizeRating("nine"), null);
  });
});

describe("isEmptyEntry", () => {
  it("recognises an entry that says nothing", () => {
    assert.equal(isEmptyEntry({ status: null, rating: null, note: null }), true);
    assert.equal(isEmptyEntry(null), true);
  });

  it("a rating alone is enough to keep", () => {
    assert.equal(isEmptyEntry({ rating: 6, note: null }), false);
  });

  it("either list alone is enough to keep", () => {
    assert.equal(isEmptyEntry({ [WANT_TO_WATCH]: true }), false);
    assert.equal(isEmptyEntry({ [WATCHED]: true }), false);
  });
});

describe("createItemStore", () => {
  it("indexes entries by title, whatever type the id arrives as", () => {
    const items = createItemStore([{ title_id: FAUDA, [WATCHED]: true }]);

    assert.equal(items.get(FAUDA)[WATCHED], true);
    assert.equal(items.get("1")[WATCHED], true);
  });

  it("reports nothing for a title that was never touched", () => {
    assert.equal(createItemStore().get(FAUDA), null);
  });

  it("applies a change immediately", () => {
    const items = createItemStore();

    items.apply(FAUDA, { [WANT_TO_WATCH]: true });

    assert.equal(items.get(FAUDA)[WANT_TO_WATCH], true);
  });

  it("keeps the fields a change did not mention", () => {
    const items = createItemStore([
      { title_id: FAUDA, [WATCHED]: true, rating: 9, note: "hi" },
    ]);

    items.apply(FAUDA, { rating: 10 });

    assert.equal(items.get(FAUDA)[WATCHED], true);
    assert.equal(items.get(FAUDA).note, "hi");
    assert.equal(items.get(FAUDA).rating, 10);
  });

  it("drops an entry once nothing is left in it", () => {
    const items = createItemStore([{ title_id: FAUDA, [WATCHED]: true }]);

    items.apply(FAUDA, { [WATCHED]: false });

    assert.equal(items.get(FAUDA), null);
    assert.equal(items.size, 0);
  });

  it("hands back an undo that restores the previous entry exactly", () => {
    const items = createItemStore([
      { title_id: FAUDA, [WATCHED]: true, rating: 8, note: null },
    ]);

    const rollback = items.apply(FAUDA, { rating: 3 });
    rollback();

    assert.deepEqual(items.get(FAUDA), {
      title_id: FAUDA,
      [WATCHED]: true,
      rating: 8,
      note: null,
    });
  });

  it("undoes an addition by removing it again", () => {
    const items = createItemStore();

    const rollback = items.apply(FAUDA, { [WATCHED]: true });
    rollback();

    assert.equal(items.get(FAUDA), null);
  });

  it("undoes a removal by putting the entry back", () => {
    const items = createItemStore([{ title_id: FAUDA, [WATCHED]: true, rating: null }]);

    const rollback = items.apply(FAUDA, { [WATCHED]: false });
    rollback();

    assert.equal(items.get(FAUDA)[WATCHED], true);
  });

  it("replaces everything when the server has spoken", () => {
    const items = createItemStore([{ title_id: FAUDA, [WATCHED]: true }]);

    items.replaceAll([{ title_id: 2, [WANT_TO_WATCH]: true }]);

    assert.equal(items.get(FAUDA), null);
    assert.equal(items.get(2)[WANT_TO_WATCH], true);
  });

  it("holds a title that is on both lists", () => {
    /* Watched, and worth watching again: the store keeps both flags because
     * setting one never mentions the other. */
    const items = createItemStore([{ title_id: FAUDA, [WATCHED]: true }]);

    items.apply(FAUDA, { [WANT_TO_WATCH]: true });

    assert.equal(items.get(FAUDA)[WATCHED], true);
    assert.equal(items.get(FAUDA)[WANT_TO_WATCH], true);
  });

  it("keeps an entry that is still on the other list", () => {
    const items = createItemStore([
      { title_id: FAUDA, [WATCHED]: true, [WANT_TO_WATCH]: true },
    ]);

    items.apply(FAUDA, { [WATCHED]: false });

    assert.equal(items.get(FAUDA)[WANT_TO_WATCH], true);
    assert.equal(items.size, 1);
  });

  it("empties on sign-out", () => {
    const items = createItemStore([{ title_id: FAUDA, status: WATCHED }]);

    items.clear();

    assert.equal(items.size, 0);
  });
});

describe("myItemsQuery", () => {
  it("asks for one list", () => {
    assert.equal(myItemsQuery({ status: WATCHED }), "status=watched&page=1&page_size=24");
  });

  it("asks for everything rated", () => {
    assert.equal(myItemsQuery({ rated: true }), "rated=true&page=1&page_size=24");
  });

  it("omits filters that are not set", () => {
    assert.equal(myItemsQuery(), "page=1&page_size=24");
  });

  it("asks about one page of the catalog", () => {
    /* Repeated rather than comma-joined: that is the shape FastAPI reads a
     * list of query values in. */
    assert.equal(
      myItemsQuery({ titleIds: [1, 2] }, { pageSize: 2 }),
      "title_ids=1&title_ids=2&page=1&page_size=2",
    );
  });

  it("cannot express an empty list, which is why nobody may ask with one", () => {
    /* No ids at all reads as "no filter" at the far end - every entry the user
     * has. The caller checks it has something to ask about first. */
    assert.equal(myItemsQuery({ titleIds: [] }), "page=1&page_size=24");
  });
});
