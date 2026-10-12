import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  ANY_SERVICE,
  forYouReason,
  picksFilters,
  picksSearch,
  sharedReason,
} from "../js/picks.js";

const t = (key, values = {}) =>
  `${key}:${Object.entries(values)
    .map(([name, value]) => `${name}=${value}`)
    .join(",")}`;

describe("forYouReason", () => {
  const pick = { seed: { name: "Reservoir Dogs", name_he: "כלבי אשמורת", rating: 10 } };

  it("names the favourite and the rating it was given", () => {
    assert.equal(
      forYouReason(pick, "en", t),
      "forYou.because:title=Reservoir Dogs,rating=10",
    );
  });

  it("uses the Hebrew name for a Hebrew reader", () => {
    assert.equal(forYouReason(pick, "he", t), "forYou.because:title=כלבי אשמורת,rating=10");
  });

  it("falls back to the name it has", () => {
    const english = { seed: { name: "Stalker", name_he: null, rating: 9 } };
    assert.equal(forYouReason(english, "he", t), "forYou.because:title=Stalker,rating=9");
  });
});

describe("sharedReason", () => {
  it("leads with the people, at most two", () => {
    const card = {
      because: {
        people: [
          { name_en: "Quentin Tarantino" },
          { name_en: "Harvey Keitel" },
          { name_en: "Tim Roth" },
        ],
        genres: ["Crime"],
      },
    };
    assert.equal(sharedReason(card, "en", t), "similar.with:names=Quentin Tarantino, Harvey Keitel");
  });

  it("falls back to the genres when no one is shared", () => {
    assert.equal(
      sharedReason({ because: { people: [], genres: ["Crime", "Drama"] } }, "en", t),
      "Crime · Drama",
    );
  });

  it("names the genres in Hebrew for a Hebrew reader", () => {
    const card = { because: { genres: ["Drama"], genres_he: ["דרמה"] } };
    assert.equal(sharedReason(card, "he", t), "דרמה");
    assert.equal(sharedReason(card, "en", t), "Drama");
  });

  it("says nothing rather than something empty", () => {
    assert.equal(sharedReason({}, "en", t), "");
  });
});

describe("picksFilters", () => {
  const mine = ["netflix_il", "yes"];

  it("opens on the member's own services", () => {
    assert.deepEqual(picksFilters("", mine).sources, mine);
  });

  it("lets the address say otherwise, including everywhere", () => {
    assert.deepEqual(picksFilters("sources=hot", mine).sources, ["hot"]);
    assert.deepEqual(picksFilters(`sources=${ANY_SERVICE}`, mine).sources, []);
  });

  it("keeps only what a pick is narrowed and ordered by", () => {
    const filters = picksFilters(
      "q=heat&type=movie&genres=3,9&score_min=70&runtime_max=120&sort=year&year_min=1990&order=asc",
      [],
    );
    assert.deepEqual(
      [filters.type, filters.genres, filters.scoreMin, filters.runtimeMax, filters.sort],
      ["movie", ["3", "9"], "70", "120", "year"],
    );
    assert.deepEqual([filters.q, filters.yearMin, filters.order], ["", "", ""]);
  });

  it("falls back to best match for an order it does not have", () => {
    assert.equal(picksFilters("sort=name", []).sort, "");
  });
});

describe("picksSearch", () => {
  const mine = ["netflix_il", "yes"];
  const round = (search) => picksSearch(picksFilters(search, mine), mine);

  it("says nothing about services while they are the member's own", () => {
    assert.equal(round(""), "");
    assert.equal(round("sort=score"), "sort=score");
  });

  it("says everywhere out loud, so it survives coming back", () => {
    assert.equal(picksSearch({ ...picksFilters("", mine), sources: [] }, mine), "sources=any");
    assert.equal(round("sources=any"), "sources=any");
  });

  it("has nothing to say for a member with no services saved", () => {
    assert.equal(picksSearch(picksFilters("", []), []), "");
  });

  it("carries any other choice as it is", () => {
    assert.equal(round("sources=hot&type=series"), "sources=hot&type=series");
  });
});
