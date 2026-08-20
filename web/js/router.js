/* Hash routing.
 *
 * The hash keeps every view shareable and bookmarkable without needing server
 * rewrites - the API and the client are one origin, and a deep link should not
 * depend on which one answers first.
 */

/**
 * Split a hash into a route name and its parts.
 *
 * `#/title/42?x=1` becomes `{ name: "title", params: ["42"], search: "x=1" }`.
 */
export function parseHash(hash = "") {
  const raw = hash.replace(/^#/, "");
  const [path, search = ""] = raw.split("?");
  const segments = path.split("/").filter(Boolean);

  return {
    name: segments[0] ?? "home",
    params: segments.slice(1),
    search,
  };
}

/** Build a hash from a route name, parts and query state. */
export function buildHash(name, params = [], search = "") {
  const path = [name === "home" ? "" : name, ...params].filter(Boolean).join("/");
  const query = String(search);
  return `#/${path}${query ? `?${query}` : ""}`;
}

export function createRouter(routes, { onChange } = {}) {
  let teardown = null;
  let listening = false;

  async function render() {
    const route = parseHash(window.location.hash);
    const view = routes[route.name] ?? routes.notFound;

    if (teardown) {
      teardown();
      teardown = null;
    }

    onChange?.(route);
    teardown = (await view(route)) ?? null;
  }

  function start() {
    if (!listening) {
      window.addEventListener("hashchange", render);
      listening = true;
    }
    return render();
  }

  /**
   * Stop responding to the hash, and tear down whatever is on screen.
   *
   * The app rebuilds itself when the language changes or a session ends, which
   * means a fresh router over a fresh `<main>`. Without this the old router
   * keeps its `hashchange` listener and goes on rendering into the element it
   * captured - which is no longer in the document, so every later link would
   * change the URL and nothing else.
   */
  function stop() {
    if (listening) {
      window.removeEventListener("hashchange", render);
      listening = false;
    }
    if (teardown) {
      teardown();
      teardown = null;
    }
  }

  /**
   * Change the query without adding a history entry.
   *
   * Typing in the search box should not fill the back button with one entry
   * per keystroke.
   */
  function replaceSearch(search) {
    const route = parseHash(window.location.hash);
    const next = buildHash(route.name, route.params, search);
    window.history.replaceState(null, "", next);
  }

  function navigate(name, params = [], search = "") {
    window.location.hash = buildHash(name, params, search);
  }

  return { start, stop, render, navigate, replaceSearch };
}
