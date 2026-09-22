import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { presetApplied, presetKeys, toggleMine } from "../js/views/home.js";

/* "My services" is edited from the catalog's services dropdown as well as from
 * settings. The dropdown lists only services with titles right now, so an edit
 * made there must not quietly drop one it does not show. */

const SOURCES = [
  { id: 1, key: "netflix" },
  { id: 2, key: "yes" },
  { id: 3, key: "retired" },
];

describe("toggleMine", () => {
  it("adds a service", () => {
    assert.deepEqual(toggleMine([1], 2, true), [1, 2]);
  });

  it("takes one out and keeps the rest, listed or not", () => {
    assert.deepEqual(toggleMine([1, 2, 3], 2, false), [1, 3]);
  });

  it("does not add a service twice", () => {
    assert.deepEqual(toggleMine([1, 2], 2, true), [1, 2]);
  });

  it("starts from nothing for somebody who never picked any", () => {
    assert.deepEqual(toggleMine(undefined, 1, true), [1]);
  });
});

describe("the my-services preset", () => {
  it("is the saved ids as catalog keys, skipping ids it does not know", () => {
    assert.deepEqual(presetKeys({ my_source_ids: [2, 9, 1] }, SOURCES), ["yes", "netflix"]);
  });

  it("is applied only when the filter is exactly the preset", () => {
    const user = { my_source_ids: [1, 2] };
    const on = (sources) => presetApplied({ user, sources: SOURCES, state: { filters: { sources } } });

    assert.equal(on(["yes", "netflix"]), true);
    assert.equal(on(["netflix"]), false);
    assert.equal(on(["netflix", "yes", "retired"]), false);
  });

  it("is never applied when there is nothing saved", () => {
    const state = { filters: { sources: [] } };
    assert.equal(presetApplied({ user: { my_source_ids: [] }, sources: SOURCES, state }), false);
  });
});
