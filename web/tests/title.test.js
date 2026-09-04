import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { translate } from "../js/i18n.js";
import { scoreCaption } from "../js/ui.js";
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
  const title = {
    rating_groups: [
      {
        key: "rt",
        name: "Rotten Tomatoes",
        scores: [
          { provider: "rt_critics", provider_name: "Tomatometer" },
          { provider: "rt_audience", provider_name: "Audience" },
        ],
      },
      { key: "imdb", name: "IMDb", scores: [{ provider: "imdb", provider_name: "IMDb" }] },
    ],
    ratings: [{ provider: "edb", provider_name: "EDB" }],
  };

  it("names the service too when it reported more than one figure", () => {
    // A table with a row called "Audience" and another called "צופים" is a
    // table where two different sites' crowds cannot be told apart. The chip
    // can be terse because the logo above it says which site; a row cannot.
    assert.equal(providerName("rt_audience", title), "Rotten Tomatoes · Audience");
  });

  it("leaves a lone figure to its own name", () => {
    assert.equal(providerName("imdb", title), "IMDb");
  });

  it("still reads a rater that is only in the flat list", () => {
    assert.equal(providerName("edb", title), "EDB");
  });

  it("falls back to the stored key rather than an empty cell", () => {
    assert.equal(providerName("tmdb", title), "tmdb");
  });

  it("copes with a title carrying no ratings at all", () => {
    assert.equal(providerName("imdb", { ratings: [], rating_groups: [] }), "imdb");
    assert.equal(providerName("imdb", undefined), "imdb");
  });
});

/* The one decision inside a rating chip.
 *
 * Everything else it renders arrives in the payload - the mark, the name, the
 * order - because the client is not taught providers any more. What is left to
 * decide is whether a figure needs saying what it is, and that depends only on
 * whether it has a sibling. */

describe("scoreCaption", () => {
  const critics = { provider_name: "Tomatometer", vote_count: 430 };

  it("names a figure that shares its chip with another", () => {
    assert.equal(scoreCaption(critics, { named: true, language: "en" }), "Tomatometer · 430");
  });

  it("leaves a lone figure unnamed - the mark above it already said so", () => {
    assert.equal(scoreCaption(critics, { named: false, language: "en" }), "430");
  });

  it("says nothing at all rather than a stray separator", () => {
    const unvoted = { provider_name: "מבקרים", vote_count: null };
    assert.equal(scoreCaption(unvoted, { named: false, language: "he" }), "");
    assert.equal(scoreCaption(unvoted, { named: true, language: "he" }), "מבקרים");
  });
});
