import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  currentSources,
  filtersToParams,
  formatPrice,
  formatScore,
  offerState,
  paramsToFilters,
  scoreBand,
  sourceColorVar,
} from "../js/format.js";

describe("scoreBand", () => {
  it("bands a score by the thresholds the colours use", () => {
    assert.equal(scoreBand(90), "high");
    assert.equal(scoreBand(75), "high");
    assert.equal(scoreBand(74), "mid");
    assert.equal(scoreBand(50), "mid");
    assert.equal(scoreBand(49), "low");
    assert.equal(scoreBand(0), "low");
  });

  it("treats an absent score as its own band", () => {
    // "Not rated yet" and "rated badly" must not look the same.
    assert.equal(scoreBand(null), "none");
    assert.equal(scoreBand(undefined), "none");
  });
});

describe("formatScore", () => {
  it("shows a dash rather than a zero when there is no score", () => {
    assert.equal(formatScore(null), "-");
    assert.equal(formatScore(0), "0");
    assert.equal(formatScore(84), "84");
  });
});

describe("formatPrice", () => {
  it("reads minor units as the currency's own amount", () => {
    // 1990 agorot is ₪19.90, and the symbol comes from the currency, not us.
    const price = formatPrice(1990, "ILS", "en");
    assert.match(price, /19\.90/);
    assert.match(price, /₪/);
  });

  it("says nothing when a source charges nothing per title", () => {
    // A subscription offer has no price; "₪0.00" would be a lie.
    assert.equal(formatPrice(null, "ILS"), "");
    assert.equal(formatPrice(undefined, undefined), "");
    assert.equal(formatPrice(1990, null), "");
  });

  it("shows a free offer as free rather than as nothing", () => {
    assert.match(formatPrice(0, "ILS", "en"), /0\.00/);
  });

  it("still shows the amount when the currency code is malformed", () => {
    // Intl formats any well-formed code, known or not, and throws on the rest;
    // a bad code in the data must not blank out the price.
    assert.match(formatPrice(1990, "XYZ", "en"), /19\.90/);
    assert.equal(formatPrice(1990, "shekel", "en"), "19.90 shekel");
  });
});

describe("sourceColorVar", () => {
  it("maps a source key to its custom property", () => {
    assert.equal(sourceColorVar("netflix_il"), "var(--source-netflix_il, var(--source-unknown))");
  });

  it("refuses anything that could break out of the value", () => {
    assert.equal(sourceColorVar("evil; color: red"), "var(--source-unknown)");
    assert.equal(sourceColorVar(undefined), "var(--source-unknown)");
  });
});

describe("currentSources", () => {
  const offer = (key, extra = {}) => ({
    source_key: key,
    is_current: true,
    source_active: true,
    ...extra,
  });

  it("lists only what can be watched now", () => {
    const sources = currentSources([
      offer("mako"),
      offer("netflix_il", { is_current: false }),
      offer("free_tv", { source_active: false }),
    ]);

    assert.deepEqual(sources, ["mako"]);
  });

  it("deduplicates a service offering several formats", () => {
    assert.deepEqual(currentSources([offer("hot"), offer("hot")]), ["hot"]);
  });

  it("is stable, so a title's colours do not reshuffle between renders", () => {
    assert.deepEqual(currentSources([offer("yes_plus"), offer("cellcom_tv")]), [
      "cellcom_tv",
      "yes_plus",
    ]);
  });

  it("handles a title on nothing", () => {
    assert.deepEqual(currentSources([]), []);
  });
});

describe("offerState", () => {
  it("distinguishes the two ways a title stops being watchable", () => {
    assert.equal(offerState({ is_current: true, source_active: true }), "available");
    assert.equal(offerState({ is_current: false, source_active: true }), "gone");
    assert.equal(offerState({ is_current: true, source_active: false }), "untracked");
  });

  it("reports an untracked source even when the offer looks live", () => {
    // We stopped watching that service, so we cannot vouch for it either way.
    assert.equal(offerState({ is_current: false, source_active: false }), "untracked");
  });
});

describe("filter round-tripping", () => {
  it("omits defaults so a plain URL stays clean", () => {
    const params = filtersToParams({ q: "", sources: [], available: "current", sort: "score" });

    assert.equal(params.toString(), "");
  });

  it("survives a round trip", () => {
    const filters = {
      q: "פאודה",
      sources: ["mako", "netflix_il"],
      type: "series",
      available: "any",
      sort: "year",
    };

    assert.deepEqual(paramsToFilters(filtersToParams(filters).toString()), filters);
  });

  it("falls back to sensible defaults for an empty query string", () => {
    assert.deepEqual(paramsToFilters(""), {
      q: "",
      sources: [],
      type: "",
      available: "current",
      sort: "score",
    });
  });
});
