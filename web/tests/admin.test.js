import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { reviewsQuery, runsQuery } from "../js/api.js";
import {
  formatPercent,
  percentBand,
  share,
  statEntries,
} from "../js/views/manage.js";

/* The two query builders behind the Manage tab.
 *
 * Worth testing on their own because both feed URLs that end up in the address
 * bar and in a back button: a filter that silently sends an empty parameter is
 * a different URL for the same view, and a default that is sent explicitly
 * makes every "no filters" link look like a filtered one. */

describe("runsQuery", () => {
  it("asks for the first page by default", () => {
    const params = new URLSearchParams(runsQuery());

    assert.equal(params.get("page"), "1");
    assert.equal(params.get("page_size"), "25");
  });

  it("leaves an unset filter out entirely rather than sending it empty", () => {
    const params = new URLSearchParams(runsQuery({ source: "", status: "" }));

    assert.equal(params.has("source"), false);
    assert.equal(params.has("status"), false);
    assert.equal(params.has("phase"), false);
  });

  it("carries the filters that are set", () => {
    const params = new URLSearchParams(runsQuery({ source: "mako", status: "failed" }));

    assert.equal(params.get("source"), "mako");
    assert.equal(params.get("status"), "failed");
  });

  it("takes the page it is given", () => {
    const params = new URLSearchParams(runsQuery({}, { page: 3, pageSize: 10 }));

    assert.equal(params.get("page"), "3");
    assert.equal(params.get("page_size"), "10");
  });
});

describe("reviewsQuery", () => {
  it("defaults to the oldest first, which is what the queue means", () => {
    assert.equal(new URLSearchParams(reviewsQuery()).get("order"), "age");
  });

  it("carries a chosen order", () => {
    const params = new URLSearchParams(reviewsQuery({ order: "similarity" }));

    assert.equal(params.get("order"), "similarity");
  });

  it("narrows to one source when asked", () => {
    const params = new URLSearchParams(reviewsQuery({ source: "freetv" }));

    assert.equal(params.get("source"), "freetv");
  });

  it("does not send an empty source", () => {
    assert.equal(new URLSearchParams(reviewsQuery({ source: "" })).has("source"), false);
  });
});

describe("statEntries", () => {
  it("keeps a flat tally as it is", () => {
    assert.deepEqual(statEntries({ downloaded: 11, skipped: 900 }), [
      ["downloaded", "11"],
      ["skipped", "900"],
    ]);
  });

  it("flattens a map rather than saying [object Object]", () => {
    /* `by_enricher` and `matched_by` are maps, and the one thing a panel of
     * numbers must never show is the string "[object Object]". */
    const entries = statEntries({ by_enricher: { tmdb: 900, imdb: 208 } });

    assert.deepEqual(entries, [
      ["by enricher · tmdb", "900"],
      ["by enricher · imdb", "208"],
    ]);
  });

  it("counts a list instead of dropping it", () => {
    assert.deepEqual(statEntries({ errors: ["boom", "bang"] }), [["errors", "2"]]);
  });

  it("does not say the same count twice", () => {
    /* The sync and enrich tallies record error_count beside errors. */
    assert.deepEqual(statEntries({ errors: ["boom"], error_count: 1 }), [["error count", "1"]]);
  });

  it("leaves an empty map or list out, so a clean run reads as one", () => {
    assert.deepEqual(statEntries({ downloaded: 3, errors: [], matched_by: {} }), [
      ["downloaded", "3"],
    ]);
  });

  it("keeps a zero, which is a real answer", () => {
    assert.deepEqual(statEntries({ items_seen: 0 }), [["items seen", "0"]]);
  });

  it("survives a run with no stats at all", () => {
    assert.deepEqual(statEntries(undefined), []);
    assert.deepEqual(statEntries({}), []);
  });

  it("skips a null without rendering the word", () => {
    assert.deepEqual(statEntries({ finished: null, seen: 2 }), [["seen", "2"]]);
  });
});

describe("percentBand", () => {
  /* The colours only mean anything because every figure they sit on is stated
   * so that more is better - "with a poster" rather than "no poster". */
  it("is green above 95", () => {
    assert.equal(percentBand(95.1), "good");
    assert.equal(percentBand(100), "good");
  });

  it("is amber above 75 and up to 95", () => {
    assert.equal(percentBand(95), "warn");
    assert.equal(percentBand(75.1), "warn");
  });

  it("is red at 75 and below", () => {
    assert.equal(percentBand(75), "bad");
    assert.equal(percentBand(0), "bad");
  });

  it("has no verdict on a missing figure", () => {
    assert.equal(percentBand(null), "none");
    assert.equal(percentBand(undefined), "none");
  });
});

describe("share", () => {
  it("is the part over the whole", () => {
    assert.equal(share(1, 4), 25);
  });

  it("is nothing at all when there is no whole", () => {
    /* A source that has never synced has no completeness. Calling it 0% would
     * read as "everything is missing" and paint the row red. */
    assert.equal(share(0, 0), null);
  });
});

describe("formatPercent", () => {
  it("rounds to whole numbers, which is how a dashboard is read", () => {
    assert.equal(formatPercent(96.4), "96%");
    assert.equal(formatPercent(75.5), "76%");
  });

  it("keeps a decimal below ten", () => {
    /* Rounding 0.7% to 1% triples it; rounding it to 0% hides a queue that is
     * not empty. */
    assert.equal(formatPercent(0.7), "0.7%");
    assert.equal(formatPercent(9.9), "9.9%");
  });

  it("shows a dash rather than a number it does not have", () => {
    assert.equal(formatPercent(null), "—");
  });

  it("reaches 100% only when there is nothing left", () => {
    /* 3407 of 3410 cleared is 99.91%, and a column reading 100% over three
     * listings still waiting is the one figure nobody would think to check. */
    assert.equal(formatPercent((3407 / 3410) * 100), "99%");
    assert.equal(formatPercent(99.99), "99%");
    assert.equal(formatPercent(100), "100%");
  });

  it("reaches 0% only when there is nothing at all", () => {
    assert.equal(formatPercent((1 / 34024) * 100), "<0.1%");
    assert.equal(formatPercent(0), "0%");
  });
});
