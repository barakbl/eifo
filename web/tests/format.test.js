import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  DEFAULT_FILTERS,
  countryFlag,
  countryName,
  currentSources,
  filtersToParams,
  formatPrice,
  formatScore,
  languageName,
  offerState,
  personName,
  runtimeBand,
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
      ...DEFAULT_FILTERS,
      q: "פאודה",
      sources: ["mako", "netflix_il"],
      type: "series",
      available: "any",
      sort: "year",
    };

    assert.deepEqual(paramsToFilters(filtersToParams(filters).toString()), filters);
  });

  it("carries the advanced filters too, so a filtered view is shareable", () => {
    const filters = {
      ...DEFAULT_FILTERS,
      yearMin: "1980",
      yearMax: "1989",
      genres: ["18", "35"],
      scoreMin: "70",
    };

    assert.deepEqual(paramsToFilters(filtersToParams(filters).toString()), filters);
  });

  it("carries a sort direction only when it was argued with", () => {
    const natural = filtersToParams({ ...DEFAULT_FILTERS, sort: "year", order: "" });
    const flipped = filtersToParams({ ...DEFAULT_FILTERS, sort: "year", order: "asc" });

    assert.equal(natural.has("order"), false);
    assert.equal(flipped.get("order"), "asc");
  });

  it("names the year parameters the way the API does", () => {
    const params = filtersToParams({ ...DEFAULT_FILTERS, yearMin: "1980", yearMax: "1989" });

    assert.equal(params.get("year_min"), "1980");
    assert.equal(params.get("year_max"), "1989");
  });

  it("falls back to sensible defaults for an empty query string", () => {
    assert.deepEqual(paramsToFilters(""), DEFAULT_FILTERS);
  });
});

describe("languageName", () => {
  it("names a language in the reader's own language", () => {
    assert.equal(languageName("he", "he"), "עברית");
    assert.equal(languageName("he", "en"), "Hebrew");
  });

  it("falls back to the code rather than showing nothing", () => {
    // Better a reader sees "zzz" than an empty row where a fact should be.
    assert.equal(languageName("zzz", "en"), "zzz");
  });

  it("says nothing when there is no code", () => {
    assert.equal(languageName(null), "");
    assert.equal(languageName(""), "");
  });
});

describe("countryName", () => {
  it("names a country in the reader's own language", () => {
    assert.equal(countryName("IL", "he"), "ישראל");
    assert.equal(countryName("IL", "en"), "Israel");
  });

  it("says nothing when there is no code", () => {
    assert.equal(countryName(undefined), "");
  });
});

describe("personName", () => {
  it("prefers the reader's language", () => {
    const person = { name_he: "איילת מנחמי", name_en: "Ayelet Menachemi" };
    assert.equal(personName(person, "he"), "איילת מנחמי");
    assert.equal(personName(person, "en"), "Ayelet Menachemi");
  });

  it("falls back to the other language rather than leaving a name blank", () => {
    // Most of the Israeli archive has Hebrew names only, and TMDB has Latin.
    assert.equal(personName({ name_he: "איילת מנחמי" }, "en"), "איילת מנחמי");
    assert.equal(personName({ name_en: "Jon Watts" }, "he"), "Jon Watts");
  });

  it("survives a missing person", () => {
    assert.equal(personName(null, "he"), "");
  });
});

describe("runtimeBand", () => {
  it("bands a runtime the way film itself does", () => {
    assert.equal(runtimeBand(72).key, "runtime.short");
    assert.equal(runtimeBand(103).key, "runtime.standard");
    assert.equal(runtimeBand(131).key, "runtime.long");
    assert.equal(runtimeBand(195).key, "runtime.epic");
  });

  it("puts a boundary in the longer band", () => {
    // 90 minutes is a standard feature, not a short one.
    assert.equal(runtimeBand(89).key, "runtime.short");
    assert.equal(runtimeBand(90).key, "runtime.standard");
    assert.equal(runtimeBand(120).key, "runtime.long");
    assert.equal(runtimeBand(150).key, "runtime.epic");
  });

  it("fills one more dot per band", () => {
    assert.deepEqual([72, 103, 131, 195].map((m) => runtimeBand(m).level), [1, 2, 3, 4]);
  });

  it("has no band for a runtime we do not hold", () => {
    // An unknown runtime must not read as "very short".
    assert.equal(runtimeBand(0), null);
    assert.equal(runtimeBand(null), null);
    assert.equal(runtimeBand(undefined), null);
    assert.equal(runtimeBand("120"), null);
  });
});

describe("countryFlag", () => {
  it("turns an ISO code into its flag", () => {
    assert.equal(countryFlag("IL"), "🇮🇱");
    assert.equal(countryFlag("FR"), "🇫🇷");
  });

  it("accepts a lowercase code", () => {
    assert.equal(countryFlag("il"), "🇮🇱");
  });

  it("shows nothing rather than nonsense for a code that is not one", () => {
    assert.equal(countryFlag("XYZ"), "");
    assert.equal(countryFlag("I1"), "");
    assert.equal(countryFlag(null), "");
  });
});
