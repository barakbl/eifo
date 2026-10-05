import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { resultAction, resultMeta } from "../js/addfilm.js";
import { OTHER_SERVICES, presetKeys, withOtherServices } from "../js/views/home.js";

const t = (key) => `<${key}>`;

describe("resultAction", () => {
  it("adds a film the catalog does not have", () => {
    assert.deepEqual(resultAction({ tmdb_id: 603, title_id: null }), { kind: "add", tmdbId: 603 });
  });

  it("opens one it does, rather than offering to add a copy", () => {
    assert.deepEqual(resultAction({ tmdb_id: 603, title_id: 42 }), { kind: "open", titleId: 42 });
  });
});

describe("resultMeta", () => {
  it("gives the year and the name it was made under", () => {
    assert.equal(resultMeta({ year: 1999, original_name: "The Matrix" }), "1999 · The Matrix");
  });

  it("leaves out what it does not know", () => {
    assert.equal(resultMeta({ year: null, original_name: null }), "");
    assert.equal(resultMeta({ year: 2001 }), "2001");
  });
});

describe("withOtherServices", () => {
  const SOURCES = [{ id: 1, key: "netflix_il", name: "Netflix", title_count: 10 }];

  it("adds Other services last when members have added something", () => {
    const listed = withOtherServices(SOURCES, { count: 3, t });

    assert.equal(listed.length, 2);
    assert.deepEqual(listed[1], {
      key: OTHER_SERVICES,
      name: "<filters.otherServices>",
      title_count: 3,
      active: true,
      virtual: true,
    });
  });

  it("is absent while there is nothing in it", () => {
    assert.equal(withOtherServices(SOURCES, { count: 0, t }), SOURCES);
    assert.equal(withOtherServices(SOURCES, { t }), SOURCES);
  });

  it("is never mistaken for a saved service", () => {
    const listed = withOtherServices(SOURCES, { count: 3, t });

    assert.deepEqual(presetKeys({ my_source_ids: [1] }, listed), ["netflix_il"]);
  });

  it("uses the key the server filters on", () => {
    assert.equal(OTHER_SERVICES, "other");
  });
});
