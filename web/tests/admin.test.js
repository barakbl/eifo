import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { reviewsQuery, runsQuery } from "../js/api.js";

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
