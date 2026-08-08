/* The catalog grid: filters, search-as-you-type, and endless scrolling. */

import { ApiError, listTitles } from "../api.js";
import {
  currentSources,
  filtersToParams,
  paramsToFilters,
  sourceColorVar,
} from "../format.js";
import { displayName } from "../i18n.js";
import { el, replace, scorePill, skeletonCards, spine, stateBlock } from "../ui.js";

const PAGE_SIZE = 24;
// The first screen is loaded eagerly. Lazy-loading what is already visible
// only delays the largest paint, which is the metric this page is judged on.
const EAGER_IMAGES = 12;

const TYPES = ["", "movie", "series"];
const SORTS = ["score", "score_israeli", "year", "name", "recently_added"];
const AVAILABILITY = ["current", "any", "gone"];

export function createHomeView({ mount, app, router }) {
  return async function render(route) {
    const filters = paramsToFilters(route.search);
    const { t, language, sources, user } = app.get();

    const state = {
      filters,
      page: 1,
      total: 0,
      loaded: [],
      loading: false,
      done: false,
      error: null,
    };

    const grid = el("ul", { class: "grid" });
    const status = el("p", { class: "results__status" });
    const sentinel = el("div", { class: "sentinel" });
    const region = el("section", { class: "results shell" }, [status, grid, sentinel]);

    const filterBar = buildFilterBar({ state, sources, user, t, onChange: apply });
    replace(mount, filterBar.node, region);

    let observer = null;
    let requestToken = 0;

    function apply(patch) {
      Object.assign(state.filters, patch);
      state.page = 1;
      state.loaded = [];
      state.done = false;
      // A change can come from anywhere in the bar — the "my services" preset
      // sets several chips at once — so the whole bar re-reads the state.
      filterBar.sync();
      // Filters live in the URL so a filtered view can be shared or reloaded.
      router.replaceSearch(filtersToParams(state.filters).toString());
      load({ reset: true });
    }

    async function load({ reset = false } = {}) {
      if (state.loading || (state.done && !reset)) return;

      state.loading = true;
      state.error = null;
      const token = ++requestToken;

      if (reset) {
        replace(grid, skeletonCards());
        status.textContent = t("results.searching");
      }

      try {
        const page = await listTitles(state.filters, {
          page: state.page,
          pageSize: PAGE_SIZE,
        });
        // A slower earlier request must not overwrite a newer result.
        if (token !== requestToken) return;

        state.total = page.total;
        state.loaded = reset ? page.items : [...state.loaded, ...page.items];
        state.done = state.loaded.length >= page.total || page.items.length === 0;
        paint();
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
        status.textContent = "";
        replace(
          grid,
          el(
            "li",
            { style: { "grid-column": "1 / -1" } },
            stateBlock({
              title: t("empty.title"),
              body: t("empty.body"),
              actionLabel: t("empty.clear"),
              onAction: () => apply({ q: "", sources: [], type: "", available: "current" }),
            }),
          ),
        );
        return;
      }

      replace(
        status,
        el("span", { class: "results__count", text: String(state.total) }),
        el("span", { text: t("results.countLabel") }),
      );
      replace(
        grid,
        state.loaded.map((title, index) => titleCard(title, language, index)),
      );
    }

    function paintError() {
      status.textContent = "";
      const offline = state.error instanceof ApiError && state.error.offline;
      replace(
        grid,
        el(
          "li",
          { style: { "grid-column": "1 / -1" } },
          stateBlock({
            title: t("error.title"),
            body: offline ? t("error.offline") : (state.error?.detail || t("error.body")),
            actionLabel: t("error.retry"),
            onAction: () => load({ reset: true }),
          }),
        ),
      );
    }

    observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting) && !state.done && !state.error) {
        state.page += 1;
        load();
      }
    });
    observer.observe(sentinel);

    await load({ reset: true });

    return () => observer?.disconnect();
  };
}

function titleCard(title, language, index = 0) {
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

  return el(
    "li",
    {},
    el("a", { class: "card", href: `#/title/${title.id}` }, [
      el("div", { class: "card__poster" }, [poster, spine(currentSources(title.availability))]),
      el("span", { class: "card__title", text: name }),
      el("span", { class: "card__meta" }, [
        title.year ? el("span", { text: String(title.year) }) : null,
        scorePill(title.score),
      ]),
    ]),
  );
}

function buildFilterBar({ state, sources, user, t, onChange }) {
  const chosen = () => new Set(state.filters.sources);

  const chips = sources.map((source) =>
    el(
      "button",
      {
        class: "chip",
        type: "button",
        "aria-pressed": String(chosen().has(source.key)),
        "data-source": source.key,
        style: { "--source-color": sourceColorVar(source.key) },
        onClick: () => {
          const next = chosen();
          if (next.has(source.key)) next.delete(source.key);
          else next.add(source.key);
          onChange({ sources: [...next] });
        },
      },
      [
        el("span", { class: "chip__dot", "aria-hidden": "true" }),
        el("span", { text: source.name }),
        source.title_count
          ? el("span", { class: "chip__count", text: String(source.title_count) })
          : null,
      ],
    ),
  );

  const selects = [
    select({
      values: TYPES,
      current: state.filters.type,
      label: (value) => t(`filters.type.${value || "any"}`),
      onChange: (value) => onChange({ type: value }),
    }),
    select({
      values: AVAILABILITY,
      current: state.filters.available,
      label: (value) => t(`filters.available.${value}`),
      onChange: (value) => onChange({ available: value }),
    }),
    select({
      values: SORTS,
      current: state.filters.sort,
      label: (value) => t(`filters.sort.${value}`),
      onChange: (value) => onChange({ sort: value }),
    }),
  ];

  const mine = myServicesChip({ user, sources, state, t, onChange });

  const node = el(
    "div",
    { class: "filters" },
    el("div", { class: "filters__row shell" }, [
      el("span", { class: "filters__label", text: t("filters.services") }),
      mine,
      ...chips,
      ...selects,
    ]),
  );

  /** Re-read the pressed state of every chip from the filters. */
  function sync() {
    const active = chosen();
    for (const chip of chips) {
      chip.setAttribute("aria-pressed", String(active.has(chip.dataset.source)));
    }
    mine?.setAttribute?.("aria-pressed", String(presetApplied({ user, sources, state })));
  }

  return { node, sync };
}

function myServicesChip({ user, sources, state, t, onChange }) {
  if (!user) return null;

  const preset = presetKeys(user, sources);
  if (!preset.length) {
    return el("a", {
      class: "chip chip--hint",
      href: "#/settings",
      text: t("filters.myServicesEmpty"),
    });
  }

  return el("button", {
    class: "chip chip--mine",
    type: "button",
    "aria-pressed": String(presetApplied({ user, sources, state })),
    text: t("filters.myServices"),
    onClick: () =>
      onChange({ sources: presetApplied({ user, sources, state }) ? [] : preset }),
  });
}

/** The user's saved services, as source keys the catalog understands. */
function presetKeys(user, sources) {
  const byId = new Map(sources.map((source) => [source.id, source.key]));
  return (user?.my_source_ids ?? []).map((id) => byId.get(id)).filter(Boolean);
}

/** Whether the current filter is exactly the saved preset. */
function presetApplied({ user, sources, state }) {
  const preset = presetKeys(user, sources);
  const active = new Set(state.filters.sources);
  return preset.length > 0 && preset.length === active.size && preset.every((k) => active.has(k));
}

function select({ values, current, label, onChange }) {
  const node = el(
    "select",
    { class: "control", onChange: (event) => onChange(event.currentTarget.value) },
    values.map((value) =>
      el("option", { value, text: label(value), selected: value === current || undefined }),
    ),
  );
  node.value = current;
  return node;
}
