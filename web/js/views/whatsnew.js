/* `#/new` - what turned up on each service lately.
 *
 * The catalog answers "what can I watch"; this answers "what is new", which is
 * a different question and was previously unanswerable: a title that had been
 * on one service for years and landed on another last night looked exactly
 * like one that had been sitting there all along.
 */

import { ApiError, listWhatsNew } from "../api.js";
import { cardActions, loadMineFor } from "../account.js";
import { formatWhen, sourceColorVar } from "../format.js";
import { displayName } from "../i18n.js";
import { el, replace, scorePill, skeletonCards, stateBlock } from "../ui.js";

const PAGE_SIZE = 24;

export function createWhatsNewView({ mount, app, router, items }) {
  return async function render(route) {
    const { t, language, sources, user } = app.get();

    // Only services with a catalog: a service offering nothing has nothing to
    // have added, and an option that can only ever answer "nothing" is a trap.
    const shown = sources.filter((source) => source.title_count > 0);
    const asked = new URLSearchParams(route.search).get("sources") ?? "";
    const state = {
      // One service at a time, and only one we still track - a stale link must
      // not silently filter the page down to nothing.
      source: shown.some((source) => source.key === asked) ? asked : "",
      page: 1,
      loaded: [],
      loading: false,
      done: false,
      error: null,
    };

    // A link naming a service we no longer track answers with everything; the
    // address bar should say so rather than going on claiming a filter.
    if (asked && !state.source) router.replaceSearch("");

    const grid = el("ul", { class: "grid" });
    const sentinel = el("div", { class: "sentinel" });
    const picker = servicePicker({ state, shown, t, onChange: choose });

    replace(
      mount,
      el("div", { class: "shell" }, [
        el("div", { class: "whatsnew__head" }, [
          el("div", {}, [
            el("h1", { class: "page__title", text: t("whatsnew.title") }),
            el("p", { class: "whatsnew__note", text: t("whatsnew.note") }),
          ]),
          picker,
        ]),
        el("section", { class: "results" }, [grid, sentinel]),
      ]),
    );

    let requestToken = 0;

    function choose(key) {
      state.source = key;
      state.page = 1;
      state.loaded = [];
      state.done = false;
      // In the URL, so a service's arrivals can be linked to and reloaded.
      router.replaceSearch(key ? `sources=${encodeURIComponent(key)}` : "");
      load({ reset: true });
    }

    async function load({ reset = false } = {}) {
      // A newly chosen service always goes, even with a page in flight; only
      // scrolling for the next page waits its turn.
      if (!reset && (state.loading || state.done || state.error)) return;

      state.loading = true;
      state.error = null;
      const token = ++requestToken;
      if (reset) replace(grid, skeletonCards());

      try {
        const page = await listWhatsNew(
          { sources: state.source ? [state.source] : [] },
          { page: state.page, pageSize: PAGE_SIZE },
        );
        // A slower earlier request must not overwrite a newer answer.
        if (token !== requestToken) return;

        state.loaded = reset ? page.items : [...state.loaded, ...page.items];
        state.done = state.loaded.length >= page.total || page.items.length === 0;
        paint();

        const mine = await loadMineFor(
          page.items.map((arrival) => arrival.title.id),
          { user, items },
        );
        if (mine && token === requestToken) paint();
      } catch (error) {
        if (token !== requestToken) return;
        state.error = error;
        paintError();
      } finally {
        if (token === requestToken) state.loading = false;
      }
    }

    function paint() {
      if (!state.loaded.length) {
        replace(
          grid,
          el(
            "li",
            { style: { "grid-column": "1 / -1" } },
            stateBlock({
              title: t("whatsnew.empty"),
              body: t("whatsnew.emptyBody"),
              // Nothing new on one service is not nothing new: the way out of
              // an empty page is the rest of them.
              actionLabel: state.source ? t("whatsnew.allServices") : t("mylist.browse"),
              onAction: () => (state.source ? choose("") : router.navigate("home")),
            }),
          ),
        );
        return;
      }

      replace(
        grid,
        state.loaded.map((arrival, index) =>
          arrivalCard(arrival, {
            language,
            t,
            index,
            actions: user ? cardActions({ titleId: arrival.title.id, items, t }) : null,
          }),
        ),
      );
    }

    function paintError() {
      const offline = state.error instanceof ApiError && state.error.offline;
      replace(
        grid,
        el(
          "li",
          { style: { "grid-column": "1 / -1" } },
          stateBlock({
            title: t("error.title"),
            body: offline ? t("error.offline") : state.error?.detail || t("error.body"),
            actionLabel: t("error.retry"),
            onAction: () => load({ reset: true }),
          }),
        ),
      );
    }

    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting) && !state.done && !state.error) {
        state.page += 1;
        load();
      }
    });
    observer.observe(sentinel);

    await load({ reset: true });

    return () => observer.disconnect();
  };
}

/** All services, or one of them. Whichever is chosen is in the URL. */
function servicePicker({ state, shown, t, onChange }) {
  const node = el(
    "select",
    {
      class: "control",
      "aria-label": t("filters.services"),
      onChange: (event) => onChange(event.currentTarget.value),
    },
    [
      el("option", { value: "", text: t("filters.servicesAll") }),
      ...shown.map((source) => el("option", { value: source.key, text: source.name })),
    ],
  );
  node.value = state.source;
  return node;
}

/* How many posters load eagerly, matching the catalog's own grid. */
const EAGER_IMAGES = 12;

/**
 * One arrival: the title, and the service it turned up on.
 *
 * The service is the point of the card rather than a footnote, so it is named
 * in full and dated - "new on Netflix, yesterday" is the whole story, and the
 * colour dot is the same one the catalog uses for that service.
 */
function arrivalCard(arrival, { language, t, index, actions }) {
  const title = arrival.title;
  const name = displayName(title, language);
  const eager = index < EAGER_IMAGES;
  const poster = title.poster_url
    ? el("img", {
        src: title.poster_url,
        alt: "",
        decoding: "async",
        loading: eager ? "eager" : "lazy",
        fetchpriority: eager ? "high" : "low",
      })
    : el("div", { class: "card__placeholder", text: name.slice(0, 1), "aria-hidden": "true" });

  return el("li", { class: actions ? "card-slot" : "" }, [
    el("a", { class: "card", href: `#/title/${title.id}` }, [
      el("div", { class: "card__poster" }, poster),
      el("span", { class: "card__title", text: name }),
      el("span", { class: "card__meta" }, [
        title.year ? el("span", { text: String(title.year) }) : null,
        scorePill(title.score),
      ]),
      el(
        "span",
        { class: "arrival", style: { "--source-color": sourceColorVar(arrival.source_key) } },
        [
          el("span", { class: "arrival__dot", "aria-hidden": "true" }),
          el("span", {
            class: "arrival__source",
            text: t("whatsnew.on", { source: arrival.source_name }),
          }),
          el("span", { class: "arrival__when", text: formatWhen(arrival.added_at, language) }),
        ],
      ),
    ]),
    actions,
  ]);
}
