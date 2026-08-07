/* The app shell: header, language and theme, routing, footer attribution. */

import { getMeta, listSources } from "./api.js";
import { DEFAULT_LANGUAGE, directionOf, isSupported, translator } from "./i18n.js";
import { createRouter } from "./router.js";
import { createStore, debounce } from "./store.js";
import { el, replace, stateBlock } from "./ui.js";
import { createHomeView } from "./views/home.js";
import { createTitleView } from "./views/title.js";

const SEARCH_DEBOUNCE_MS = 250;
const STORAGE_LANGUAGE = "tvil.language";
const STORAGE_THEME = "tvil.theme";

const app = createStore({
  language: readLanguage(),
  t: translator(readLanguage()),
  sources: [],
});

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

function buildHeader({ router }) {
  const { t } = app.get();

  const input = el("input", {
    class: "search__input",
    id: "search",
    type: "search",
    autocomplete: "off",
    placeholder: t("search.placeholder"),
    "aria-label": t("search.label"),
  });

  // One request per keystroke would be slow for the viewer and rude to the
  // server; a quarter second is long enough to finish a word.
  const onInput = debounce((value) => {
    const params = new URLSearchParams(window.location.hash.split("?")[1] ?? "");
    if (value) params.set("q", value);
    else params.delete("q");
    router.navigate("home", [], params.toString());
  }, SEARCH_DEBOUNCE_MS);

  input.addEventListener("input", (event) => onInput(event.currentTarget.value));

  const language = app.get().language;

  return el(
    "header",
    { class: "header" },
    el("div", { class: "header__inner shell" }, [
      el("a", { class: "wordmark", href: "#/", text: t("app.name") }),
      el("div", { class: "search" }, [
        input,
        el("kbd", { class: "search__hint", text: t("search.shortcut"), "aria-hidden": "true" }),
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
      ]),
    ]),
  );
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

async function start() {
  const root = document.getElementById("root");
  const main = el("main", { id: "main" });

  const router = createRouter(
    {
      home: (route) => home(route),
      title: (route) => title(route),
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

  const home = createHomeView({ mount: main, app, router });
  const title = createTitleView({ mount: main, app, router });

  const [sources, meta] = await Promise.all([
    listSources().catch(() => []),
    getMeta().catch(() => null),
  ]);
  app.set({ sources });

  replace(root, buildHeader({ router }), main, buildFooter(meta));

  if (!started) {
    started = true;
    // "/" focuses search, the way a catalog is expected to behave.
    document.addEventListener("keydown", (event) => {
      if (event.key === "/" && !isTypingTarget(event.target)) {
        event.preventDefault();
        document.getElementById("search")?.focus();
      }
    });
    await router.start();
  } else {
    await router.render();
  }
}

function isTypingTarget(node) {
  return node instanceof HTMLElement && ["INPUT", "TEXTAREA", "SELECT"].includes(node.tagName);
}

applyLanguage(app.get().language);
applyTheme(safeRead(STORAGE_THEME));
start();
