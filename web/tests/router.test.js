import assert from "node:assert/strict";
import { beforeEach, describe, it } from "node:test";

import { buildHash, createRouter, parseHash } from "../js/router.js";

/** A window stand-in: records listeners so a test can fire `hashchange` itself. */
function fakeWindow() {
  const listeners = new Map();
  return {
    location: { hash: "" },
    history: {
      replaceState(_state, _title, url) {
        this.calls.push(url);
      },
      calls: [],
    },
    scrollTo() {},
    addEventListener(type, handler) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(handler);
    },
    removeEventListener(type, handler) {
      listeners.get(type)?.delete(handler);
    },
    listenerCount(type) {
      return listeners.get(type)?.size ?? 0;
    },
    async emit(type) {
      for (const handler of [...(listeners.get(type) ?? [])]) await handler();
    },
  };
}

beforeEach(() => {
  globalThis.window = fakeWindow();
});

describe("parseHash", () => {
  it("splits a hash into a route, its parts and its query", () => {
    assert.deepEqual(parseHash("#/title/42?q=x"), { name: "title", params: ["42"], search: "q=x" });
  });

  it("treats an empty hash as home", () => {
    assert.deepEqual(parseHash(""), { name: "home", params: [], search: "" });
  });
});

describe("buildHash", () => {
  it("round-trips a route through parseHash", () => {
    assert.deepEqual(parseHash(buildHash("title", ["42"], "q=x")), {
      name: "title",
      params: ["42"],
      search: "q=x",
    });
  });

  it("writes home as a bare slash", () => {
    assert.equal(buildHash("home"), "#/");
  });
});

describe("createRouter", () => {
  it("renders the current route on start", async () => {
    const seen = [];
    window.location.hash = "#/settings";
    await createRouter({
      settings: (route) => {
        seen.push(route.name);
      },
    }).start();

    assert.deepEqual(seen, ["settings"]);
  });

  it("renders again when the hash changes", async () => {
    const seen = [];
    const router = createRouter({
      home: () => {
        seen.push("home");
      },
    });
    await router.start();
    await window.emit("hashchange");

    assert.deepEqual(seen, ["home", "home"]);
  });

  it("listens once however often it is started", async () => {
    const router = createRouter({ home: () => {} });
    await router.start();
    await router.start();

    assert.equal(window.listenerCount("hashchange"), 1);
  });

  it("stops rendering once it is retired", async () => {
    const seen = [];
    const router = createRouter({
      home: () => {
        seen.push("home");
      },
    });
    await router.start();
    router.stop();
    await window.emit("hashchange");

    assert.deepEqual(seen, ["home"]);
    assert.equal(window.listenerCount("hashchange"), 0);
  });

  it("tears down the view it left on screen when it is retired", async () => {
    let torn = 0;
    const router = createRouter({ home: () => () => torn++ });
    await router.start();
    router.stop();

    assert.equal(torn, 1);
  });

  it("hands the hash to the router that replaced it", async () => {
    // The app rebuilds itself on a language change: a new router over a new
    // <main>. If the retired one kept listening it would go on rendering into
    // the element it captured, which is no longer in the document - and every
    // link would change the URL and nothing else.
    const seen = [];
    const first = createRouter({
      home: () => {
        seen.push("first");
      },
    });
    await first.start();

    first.stop();
    const second = createRouter({
      home: () => {
        seen.push("second");
      },
    });
    await second.start();

    seen.length = 0;
    await window.emit("hashchange");

    assert.deepEqual(seen, ["second"]);
    assert.equal(window.listenerCount("hashchange"), 1);
  });
});
