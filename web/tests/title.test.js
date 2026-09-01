import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { translate } from "../js/i18n.js";
import { componentNoteKey, providerName } from "../js/views/title.js";

/* The two decisions behind "how this score was computed".
 *
 * The table used to print the weight alone, and a weight of 0 is not an
 * explanation - it reads as a rater that is switched off rather than as one
 * this title has too few votes to trust. Both of those are real states and
 * they mean opposite things, so the reason is now spelled out beside the
 * number, and these are the two functions that decide what it says. */

describe("componentNoteKey", () => {
  it("says a thinly voted rating was left out entirely", () => {
    const key = componentNoteKey({ weight: 0, damped: false, excluded: true });

    assert.equal(key, "title.notCountedTooFewVotes");
    assert.equal(translate("en", key), "not counted - too few votes");
  });

  it("says a lightly voted one was counted half", () => {
    const key = componentNoteKey({ weight: 0.5, damped: true, excluded: false });

    assert.equal(key, "title.countedHalfThinVotes");
    assert.equal(translate("en", key), "counted half - thin votes");
  });

  it("never confuses the two: excluded is not merely damped", () => {
    assert.notEqual(
      componentNoteKey({ excluded: true, damped: false }),
      componentNoteKey({ excluded: false, damped: true }),
    );
  });

  it("says nothing when the weight speaks for itself", () => {
    assert.equal(componentNoteKey({ weight: 3, damped: false, excluded: false }), null);
  });

  it("says nothing for an aggregate stored before either flag existed", () => {
    assert.equal(componentNoteKey({ weight: 3 }), null);
  });

  it("has a string in both languages", () => {
    for (const detail of [{ excluded: true }, { damped: true }]) {
      const key = componentNoteKey(detail);
      assert.notEqual(translate("he", key), key, `he is missing ${key}`);
      assert.notEqual(translate("en", key), key, `en is missing ${key}`);
    }
  });
});

describe("providerName", () => {
  const ratings = [
    { provider: "seret_viewers", provider_name: "סרט - צופים" },
    { provider: "imdb", provider_name: "IMDb" },
  ];

  it("names a rater the way the ratings beside it do", () => {
    assert.equal(providerName("seret_viewers", ratings), "סרט - צופים");
  });

  it("falls back to the stored key rather than an empty cell", () => {
    assert.equal(providerName("rt_critics", ratings), "rt_critics");
  });

  it("copes with a title carrying no ratings at all", () => {
    assert.equal(providerName("imdb", []), "imdb");
    assert.equal(providerName("imdb", undefined), "imdb");
  });
});
