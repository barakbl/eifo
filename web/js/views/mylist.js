/* `#/me` - want to watch, watched, and everything rated. */

import { listMyItems } from "../api.js";
import { titleActions } from "../account.js";
import { currentSources } from "../format.js";
import { displayName } from "../i18n.js";
import { WANT_TO_WATCH, WATCHED } from "../items.js";
import { el, replace, scorePill, skeletonCards, spine, stateBlock } from "../ui.js";

const PAGE_SIZE = 24;

const TABS = [
  { key: "want", label: "mylist.tab.want", filters: { status: WANT_TO_WATCH } },
  { key: "watched", label: "mylist.tab.watched", filters: { status: WATCHED } },
  { key: "rated", label: "mylist.tab.rated", filters: { rated: true } },
];

export function createMyListView({ mount, app, router, items }) {
  return async function render(route) {
    const { t, language, user } = app.get();

    if (!user) {
      replace(
        mount,
        el(
          "div",
          { class: "shell" },
          stateBlock({
            title: t("item.signInToTrack"),
            actionLabel: t("mylist.browse"),
            onAction: () => router.navigate("home"),
          }),
        ),
      );
      return null;
    }

    const params = new URLSearchParams(route.search);
    const tab = TABS.find((entry) => entry.key === params.get("tab")) ?? TABS[0];

    const grid = el("ul", { class: "grid" });
    replace(
      mount,
      el("div", { class: "shell" }, [
        el("h1", { class: "page__title", text: t("mylist.title") }),
        tabStrip(tab, t, router),
        el("section", { class: "results" }, grid),
      ]),
    );

    replace(grid, skeletonCards(6));

    let page;
    try {
      page = await listMyItems(tab.filters, { pageSize: PAGE_SIZE });
    } catch (error) {
      replace(
        grid,
        el(
          "li",
          { style: { "grid-column": "1 / -1" } },
          stateBlock({ title: t("error.title"), body: error?.detail || t("error.body") }),
        ),
      );
      return null;
    }

    // Everything the server returned is authoritative for the shared store, so
    // toggling from here and from a title page cannot drift apart.
    items.replaceAll(page.items);

    if (!page.items.length) {
      replace(
        grid,
        el(
          "li",
          { style: { "grid-column": "1 / -1" } },
          stateBlock({
            title: t("mylist.empty"),
            body: t("mylist.emptyBody"),
            actionLabel: t("mylist.browse"),
            onAction: () => router.navigate("home"),
          }),
        ),
      );
      return null;
    }

    replace(
      grid,
      page.items.map((entry) => entryCard(entry, { t, language, items })),
    );
    return null;
  };
}

function tabStrip(current, t, router) {
  return el(
    "div",
    { class: "tabs", role: "tablist" },
    TABS.map((tab) =>
      el("button", {
        class: `tab${tab.key === current.key ? " tab--on" : ""}`,
        type: "button",
        role: "tab",
        "aria-selected": String(tab.key === current.key),
        text: t(tab.label),
        onClick: () => router.navigate("me", [], `tab=${tab.key}`),
      }),
    ),
  );
}

function entryCard(entry, { t, language, items }) {
  const title = entry.title;
  if (!title) return null;

  const name = displayName(title, language);
  const poster = title.poster_url
    ? el("img", { src: title.poster_url, alt: "", decoding: "async", loading: "lazy" })
    : el("div", { class: "card__placeholder", text: name.slice(0, 1), "aria-hidden": "true" });

  return el("li", {}, [
    el("a", { class: "card", href: `#/title/${title.id}` }, [
      el("div", { class: "card__poster" }, [poster, spine(currentSources(title.availability))]),
      el("span", { class: "card__title", text: name }),
      el("span", { class: "card__meta" }, [
        title.year ? el("span", { text: String(title.year) }) : null,
        entry.rating != null
          ? el("span", { class: "mine", text: `★ ${entry.rating}` })
          : scorePill(title.score),
      ]),
    ]),
    // Quick actions inline, so a list can be worked through without opening
    // every title in it.
    titleActions({ titleId: title.id, items, t }),
  ]);
}
