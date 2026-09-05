/* The app shell: header, language and theme, routing, footer attribution. */

import { getAuthContext, getMe, getMeta, listSources, loginUrl, logout, setCsrfToken } from "./api.js";
import { accountMenu } from "./account.js";
import { DEFAULT_LANGUAGE, directionOf, isSupported, translator } from "./i18n.js";
import { createItemStore } from "./items.js";
import { createRouter, parseHash } from "./router.js";
import { createStore, debounce } from "./store.js";
import { createSuggest } from "./suggest.js";
import { QUERY_EVENT } from "./views/home.js";
import { el, replace, stateBlock } from "./ui.js";
import { createHomeView } from "./views/home.js";
import { createManageView } from "./views/manage.js";
import { createMyListView } from "./views/mylist.js";
import { createPersonView } from "./views/person.js";
import { createSettingsView } from "./views/settings.js";
import { createTitleView } from "./views/title.js";
import { createWhatsNewView } from "./views/whatsnew.js";

const SEARCH_DEBOUNCE_MS = 250;
const STORAGE_LANGUAGE = "eifo.language";
const STORAGE_THEME = "eifo.theme";

/* Preferences lived under a "tvil." prefix before the rename. Carry them over
 * once so nobody loses their chosen services, theme or language; this can go
 * once instances have run a newer version for a while. */
function migrateLegacyStorage() {
  try {
    for (const key of Object.keys(window.localStorage)) {
      if (!key.startsWith("tvil.")) continue;
      const renamed = `eifo.${key.slice("tvil.".length)}`;
      if (window.localStorage.getItem(renamed) === null) {
        window.localStorage.setItem(renamed, window.localStorage.getItem(key));
      }
      window.localStorage.removeItem(key);
    }
  } catch {
    // Private browsing can refuse storage; the defaults below are fine.
  }
}

migrateLegacyStorage();

const app = createStore({
  language: readLanguage(),
  t: translator(readLanguage()),
  sources: [],
  user: null,
  loginProviders: [],
});

/* One store for the signed-in user's entries, shared by every view: the title
 * page and the list page must not disagree about what is on a list. */
const items = createItemStore();

function readLanguage() {
  const stored = safeRead(STORAGE_LANGUAGE);
  return isSupported(stored) ? stored : DEFAULT_LANGUAGE;
}

function safeRead(key) {
  try {
    return window.localStorage.getItem(key);
  } catch {
    // Private browsing can refuse storage; a default is fine.
    return null;
  }
}

function safeWrite(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Preference simply will not persist.
  }
}

function applyLanguage(language) {
  document.documentElement.lang = language;
  document.documentElement.dir = directionOf(language);
  app.set({ language, t: translator(language) });
  safeWrite(STORAGE_LANGUAGE, language);
}

function applyTheme(theme) {
  if (theme) {
    document.documentElement.dataset.theme = theme;
    safeWrite(STORAGE_THEME, theme);
  } else {
    delete document.documentElement.dataset.theme;
  }
}

function currentTheme() {
  const stored = safeRead(STORAGE_THEME);
  if (stored === "light" || stored === "dark") return stored;
  return window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

/** The search text the current URL is asking for. */
function queryInHash() {
  return new URLSearchParams(window.location.hash.split("?")[1] ?? "").get("q") ?? "";
}


function buildHeader({ router }) {
  const { t } = app.get();

  const input = el("input", {
    class: "search__input",
    id: "search",
    type: "search",
    autocomplete: "off",
    placeholder: t("search.placeholder"),
    "aria-label": t("search.label"),
    // A shared or reloaded link carries its search in the URL, and the box
    // showed nothing - so the only way to narrow somebody else's search was to
    // work out what it had been and type it again.
    value: queryInHash(),
  });

  // One request per keystroke would be slow for the viewer and rude to the
  // server; a quarter second is long enough to finish a word.
  const onInput = debounce((value) => {
    const params = new URLSearchParams(window.location.hash.split("?")[1] ?? "");
    if (value) params.set("q", value);
    else params.delete("q");

    if (parseHash(window.location.hash).name === "home") {
      // Already looking at the catalog: change the query in place. Navigating
      // would add a history entry per debounced keystroke - typing "fauda"
      // left two or three of them in the back button - and rebuild the whole
      // view to change one filter.
      router.replaceSearch(params.toString());
      window.dispatchEvent(new CustomEvent(QUERY_EVENT, { detail: { q: value } }));
      return;
    }
    // Coming from a title or a person: going to the catalog is a real
    // navigation, and one entry in the back button is the right number.
    router.navigate("home", [], params.toString());
  }, SEARCH_DEBOUNCE_MS);

  // Somebody who used the back button, or followed a link, has a query the box
  // knows nothing about - unless they are the one typing.
  window.addEventListener("hashchange", () => {
    if (document.activeElement !== input) input.value = queryInHash();
  });

  input.addEventListener("input", (event) => onInput(event.currentTarget.value));

  const { language, user, loginProviders } = app.get();

  return el(
    "header",
    { class: "header" },
    el("div", { class: "header__inner shell" }, [
      el("a", { class: "wordmark", href: "#/", text: t("app.name") }),
      el("div", { class: "search" }, [
        input,
        el("kbd", { class: "search__hint", text: t("search.shortcut"), "aria-hidden": "true" }),
        createSuggest({ input, router, app }).node,
      ]),
      el("div", { class: "header__actions" }, [
        el("button", {
          class: "icon-button",
          type: "button",
          text: t("lang.toggle"),
          "aria-label": t("lang.toggle"),
          onClick: () => {
            applyLanguage(language === "he" ? "en" : "he");
            start();
          },
        }),
        el("button", {
          class: "icon-button",
          type: "button",
          text: currentTheme() === "dark" ? "☀" : "☾",
          "aria-label": t("theme.toggle"),
          onClick: (event) => {
            const next = currentTheme() === "dark" ? "light" : "dark";
            applyTheme(next);
            event.currentTarget.textContent = next === "dark" ? "☀" : "☾";
          },
        }),
        accountMenu({ user, providers: loginProviders, t, onSignOut: () => signOut(router) }),
      ]),
    ]),
  );
}

async function signOut(router) {
  try {
    await logout();
  } catch {
    // The session may already be gone; either way this browser is signed out.
  }
  setCsrfToken("");
  items.clear();
  app.set({ user: null });
  router.navigate("home");
  start();
}

/**
 * A one-line report on a sign-in that did not complete.
 *
 * The callback cannot render anything itself - it is a redirect - so it says
 * what happened in the URL and the app says it out loud here.
 */
function loginNotice(t) {
  const params = new URLSearchParams(window.location.hash.split("?")[1] ?? "");
  const outcome = params.get("login");
  // Three outcomes and three sentences. "Please try again" is actively
  // unhelpful to somebody who was not invited: trying again is the one thing
  // that will never work for them.
  const said = {
    cancelled: "auth.cancelled",
    failed: "auth.failed",
    not_invited: "auth.notInvited",
  }[outcome];
  if (!said) return null;

  return el("div", { class: "notice", role: "status" }, [
    el("span", { text: t(said) }),
    el("button", {
      class: "notice__dismiss",
      type: "button",
      text: "✕",
      "aria-label": t("empty.clear"),
      onClick: (event) => event.currentTarget.closest(".notice")?.remove(),
    }),
  ]);
}

/* What a members-only instance shows somebody who is not a member.
 *
 * Deliberately not the error state. Nothing went wrong: the catalog is private
 * and they are not signed in, which is a sentence with an action in it rather
 * than a fault to retry. */
function signInWall() {
  const { t, loginProviders } = app.get();

  return el("main", { class: "shell members__wall", id: "main" }, [
    el("div", { class: "state", role: "status" }, [
      el("div", { class: "state__mark", "aria-hidden": "true" }),
      el("p", { class: "state__title", text: t("members.wallTitle") }),
      el("p", { class: "state__body", text: t("members.wallBody") }),
      el(
        "div",
        { class: "members__wallActions" },
        loginProviders.map((provider) =>
          el("a", {
            class: "button",
            href: loginUrl(provider),
            text: t("auth.signInWith", { provider: t(`auth.provider.${provider}`) }),
          }),
        ),
      ),
    ]),
  ]);
}

function buildFooter(meta) {
  const { t } = app.get();
  const credits = meta?.attribution ?? [];

  return el(
    "footer",
    { class: "footer" },
    el("div", { class: "shell" }, [
      el(
        "ul",
        {},
        credits.map((credit) =>
          el(
            "li",
            {},
            credit.url
              ? el("a", {
                  href: credit.url,
                  rel: "noopener noreferrer",
                  target: "_blank",
                  text: credit.text,
                })
              : el("span", { text: credit.text }),
          ),
        ),
      ),
    ]),
  );
}

let started = false;
/* The router owning the `hashchange` listener right now. A rebuild (language
 * change, sign-out) mounts a new `<main>`, so the previous router has to be
 * retired or it would go on rendering into the element it captured. */
let activeRouter = null;

async function start() {
  const root = document.getElementById("root");
  const main = el("main", { id: "main" });

  activeRouter?.stop();

  activeRouter = createRouter(
    {
      home: (route) => home(route),
      new: (route) => whatsNew(route),
      title: (route) => title(route),
      people: (route) => person(route),
      me: (route) => mylist(route),
      settings: (route) => settings(route),
      manage: (route) => manage(route),
      notFound: () => {
        const { t } = app.get();
        replace(
          main,
          el(
            "div",
            { class: "shell" },
            stateBlock({ title: t("title.notFound"), actionLabel: t("title.back") }),
          ),
        );
        return null;
      },
    },
    {
      onChange: () => window.scrollTo({ top: 0 }),
    },
  );
  const router = activeRouter;

  const home = createHomeView({ mount: main, app, router, items });
  const title = createTitleView({ mount: main, app, router, items });
  const mylist = createMyListView({ mount: main, app, router, items });
  const settings = createSettingsView({ mount: main, app, router, onSignedOut: () => signOut(router) });
  const person = createPersonView({ mount: main, app, router });
  const manage = createManageView({ mount: main, app, router });
  const whatsNew = createWhatsNewView({ mount: main, app, router, items });

  const [context, sources, meta, user] = await Promise.all([
    getAuthContext().catch(() => null),
    listSources().catch(() => []),
    getMeta().catch(() => null),
    getMe().catch(() => null),
  ]);
  // From the ungated endpoint, falling back to /meta. On a members-only
  // instance /meta answers 401 to a stranger, and taking the sign-in buttons
  // from it left the wall with nothing to press.
  app.set({
    sources,
    user,
    loginProviders: context?.login_providers ?? meta?.login_providers ?? [],
  });

  // Nothing to show and no way in: the catalog is private and nobody is signed
  // in. A wall rather than the router's error state, which reads as a fault -
  // this is not broken, it is closed, and the difference is a button.
  if (context?.members_only && !user) {
    replace(root, buildHeader({ router }), loginNotice(app.get().t), signInWall(), buildFooter(null));
    return;
  }

  replace(root, buildHeader({ router }), loginNotice(app.get().t), main, buildFooter(meta));

  if (!started) {
    started = true;
    // "/" focuses search, the way a catalog is expected to behave. Bound to the
    // document once, and it looks the input up per event, so a rebuild does not
    // leave a stale handler behind.
    document.addEventListener("keydown", (event) => {
      if (event.key === "/" && !isTypingTarget(event.target)) {
        event.preventDefault();
        document.getElementById("search")?.focus();
      }
    });
  }

  // Always start: this router now owns the hash, and renders the current route.
  await router.start();
}

function isTypingTarget(node) {
  return node instanceof HTMLElement && ["INPUT", "TEXTAREA", "SELECT"].includes(node.tagName);
}

applyLanguage(app.get().language);
applyTheme(safeRead(STORAGE_THEME));
start();
