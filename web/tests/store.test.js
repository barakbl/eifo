import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { createStore, debounce, isShallowEqual } from "../js/store.js";
import { buildHash, parseHash } from "../js/router.js";
import { ApiError, errorFromResponse, titlesQuery } from "../js/api.js";

describe("createStore", () => {
  it("merges a patch into state", () => {
    const store = createStore({ language: "he", sources: [] });

    store.set({ language: "en" });

    assert.equal(store.get().language, "en");
    assert.deepEqual(store.get().sources, []);
  });

  it("notifies subscribers on change", () => {
    const store = createStore({ count: 0 });
    const seen = [];
    store.subscribe((state) => seen.push(state.count));

    store.set({ count: 1 });
    store.set({ count: 2 });

    assert.deepEqual(seen, [1, 2]);
  });

  it("stays quiet when nothing actually changed", () => {
    // Typing the same search term twice should not trigger a re-render.
    const store = createStore({ q: "fauda" });
    let calls = 0;
    store.subscribe(() => (calls += 1));

    store.set({ q: "fauda" });

    assert.equal(calls, 0);
  });

  it("unsubscribes cleanly", () => {
    const store = createStore({ n: 0 });
    let calls = 0;
    const off = store.subscribe(() => (calls += 1));

    off();
    store.set({ n: 1 });

    assert.equal(calls, 0);
  });
});

describe("isShallowEqual", () => {
  it("compares arrays by contents, not identity", () => {
    assert.ok(isShallowEqual({ a: [1, 2] }, { a: [1, 2] }));
    assert.equal(isShallowEqual({ a: [1, 2] }, { a: [2, 1] }), false);
    assert.equal(isShallowEqual({ a: [1] }, { a: [1, 2] }), false);
  });

  it("notices an added key", () => {
    assert.equal(isShallowEqual({ a: 1 }, { a: 1, b: 2 }), false);
  });
});

describe("debounce", () => {
  it("runs once after calls stop", async () => {
    let calls = 0;
    const bump = debounce(() => (calls += 1), 5);

    bump();
    bump();
    bump();
    await new Promise((resolve) => setTimeout(resolve, 25));

    assert.equal(calls, 1);
  });

  it("passes the latest arguments", async () => {
    const seen = [];
    const record = debounce((value) => seen.push(value), 5);

    record("fau");
    record("fauda");
    await new Promise((resolve) => setTimeout(resolve, 25));

    assert.deepEqual(seen, ["fauda"]);
  });

  it("can be cancelled", async () => {
    let calls = 0;
    const bump = debounce(() => (calls += 1), 5);

    bump();
    bump.cancel();
    await new Promise((resolve) => setTimeout(resolve, 25));

    assert.equal(calls, 0);
  });
});

describe("routing", () => {
  it("parses a bare hash as the home route", () => {
    assert.deepEqual(parseHash("#/"), { name: "home", params: [], search: "" });
    assert.deepEqual(parseHash(""), { name: "home", params: [], search: "" });
  });

  it("parses a route with parameters and a query", () => {
    assert.deepEqual(parseHash("#/title/42?q=fauda"), {
      name: "title",
      params: ["42"],
      search: "q=fauda",
    });
  });

  it("round-trips a route", () => {
    assert.equal(buildHash("title", ["42"]), "#/title/42");
    assert.equal(buildHash("home", [], "q=x"), "#/?q=x");
    assert.equal(parseHash(buildHash("title", ["7"], "a=b")).params[0], "7");
  });
});

describe("api", () => {
  it("builds a titles query from filter state", () => {
    const query = titlesQuery(
      { q: "fauda", sources: ["mako", "hot"], available: "any" },
      { page: 2, pageSize: 24 },
    );
    const params = new URLSearchParams(query);

    assert.equal(params.get("q"), "fauda");
    assert.equal(params.get("sources"), "mako,hot");
    assert.equal(params.get("page"), "2");
  });

  it("omits filters that are not set", () => {
    const params = new URLSearchParams(titlesQuery({}, {}));

    assert.equal(params.get("q"), null);
    assert.equal(params.get("sources"), null);
  });

  it("reads the server's problem details", async () => {
    const response = {
      status: 404,
      json: async () => ({ title: "Not found", detail: "No title with id 9" }),
    };

    const error = await errorFromResponse(response);

    assert.ok(error instanceof ApiError);
    assert.equal(error.status, 404);
    assert.equal(error.message, "Not found");
    assert.equal(error.detail, "No title with id 9");
  });

  it("falls back when the error body is not JSON", async () => {
    const response = {
      status: 502,
      json: async () => {
        throw new Error("not json");
      },
    };

    const error = await errorFromResponse(response);

    assert.equal(error.status, 502);
    assert.match(error.message, /502/);
  });

  it("knows which failures are worth retrying", () => {
    assert.ok(new ApiError("x", { offline: true }).retryable);
    assert.ok(new ApiError("x", { status: 503 }).retryable);
    assert.equal(new ApiError("x", { status: 404 }).retryable, false);
  });
});
