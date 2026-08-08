import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  WANT_TO_WATCH,
  WATCHED,
  createItemStore,
  isEmptyEntry,
  nextStatus,
  normalizeRating,
} from "../js/items.js";
import { myItemsQuery } from "../js/api.js";

const FAUDA = 1;

describe("nextStatus", () => {
  it("files a title under the list that was pressed", () => {
    assert.equal(nextStatus(null, WANT_TO_WATCH), WANT_TO_WATCH);
  });

  it("takes it out when the list it is already in is pressed again", () => {
    assert.equal(nextStatus({ status: WATCHED }, WATCHED), null);
  });

  it("moves it when the other list is pressed", () => {
    assert.equal(nextStatus({ status: WANT_TO_WATCH }, WATCHED), WATCHED);
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
    assert.equal(isEmptyEntry({ status: null, rating: 6, note: null }), false);
  });
});

describe("createItemStore", () => {
  it("indexes entries by title, whatever type the id arrives as", () => {
    const items = createItemStore([{ title_id: FAUDA, status: WATCHED }]);

    assert.equal(items.get(FAUDA).status, WATCHED);
    assert.equal(items.get("1").status, WATCHED);
  });

  it("reports nothing for a title that was never touched", () => {
    assert.equal(createItemStore().get(FAUDA), null);
  });

  it("applies a change immediately", () => {
    const items = createItemStore();

    items.apply(FAUDA, { status: WANT_TO_WATCH });

    assert.equal(items.get(FAUDA).status, WANT_TO_WATCH);
  });

  it("keeps the fields a change did not mention", () => {
    const items = createItemStore([{ title_id: FAUDA, status: WATCHED, rating: 9, note: "hi" }]);

    items.apply(FAUDA, { rating: 10 });

    assert.equal(items.get(FAUDA).status, WATCHED);
    assert.equal(items.get(FAUDA).note, "hi");
    assert.equal(items.get(FAUDA).rating, 10);
  });

  it("drops an entry once nothing is left in it", () => {
    const items = createItemStore([{ title_id: FAUDA, status: WATCHED }]);

    items.apply(FAUDA, { status: null });

    assert.equal(items.get(FAUDA), null);
    assert.equal(items.size, 0);
  });

  it("hands back an undo that restores the previous entry exactly", () => {
    const items = createItemStore([{ title_id: FAUDA, status: WATCHED, rating: 8, note: null }]);

    const rollback = items.apply(FAUDA, { rating: 3 });
    rollback();

    assert.deepEqual(items.get(FAUDA), {
      title_id: FAUDA,
      status: WATCHED,
      rating: 8,
      note: null,
    });
  });

  it("undoes an addition by removing it again", () => {
    const items = createItemStore();

    const rollback = items.apply(FAUDA, { status: WATCHED });
    rollback();

    assert.equal(items.get(FAUDA), null);
  });

  it("undoes a removal by putting the entry back", () => {
    const items = createItemStore([{ title_id: FAUDA, status: WATCHED, rating: null }]);

    const rollback = items.apply(FAUDA, { status: null });
    rollback();

    assert.equal(items.get(FAUDA).status, WATCHED);
  });

  it("replaces everything when the server has spoken", () => {
    const items = createItemStore([{ title_id: FAUDA, status: WATCHED }]);

    items.replaceAll([{ title_id: 2, status: WANT_TO_WATCH }]);

    assert.equal(items.get(FAUDA), null);
    assert.equal(items.get(2).status, WANT_TO_WATCH);
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
});
