/* The catalog grid: filters, search-as-you-type, and endless scrolling. */

import { ApiError, listGenres, listTitles } from "../api.js";
import { cardActions, loadMineFor } from "../account.js";
import {
  DEFAULT_FILTERS,
  filtersToParams,
  paramsToFilters,
  sourceColorVar,
} from "../format.js";
import { el, replace, skeletonCards, stateBlock, titleCard } from "../ui.js";

const PAGE_SIZE = 24;

/**
 * How the header tells this view the search text changed.
 *
 * Typing used to navigate, which meant tearing the whole view down and
 * building it again on every debounced keystroke: a skeleton flash, any open
 * filter dropdown snapping shut, and the scroll position back to the top.
 * Changing the query is not changing the page.
 */
export const QUERY_EVENT = "eifo:query";

const TYPES = ["", "movie", "series"];
/** Minimum-score steps. Coarse on purpose: nobody filters by 63. */
const SCORES = ["", "60", "70", "80"];

/**
 * Decade shortcuts, newest first, and the open-ended one at the end.
 *
 * They fill the year boxes rather than being a mode of their own: editing the
 * boxes is what "custom" means, so there is no third state to keep in step.
 */
const DECADES = [
  { key: "2020s", min: 2020, max: 2029 },
  { key: "2010s", min: 2010, max: 2019 },
  { key: "2000s", min: 2000, max: 2009 },
  { key: "1990s", min: 1990, max: 1999 },
  { key: "1980s", min: 1980, max: 1989 },
  { key: "older", min: 1880, max: 1979 },
];
/**
 * "I have an evening this long" - the one filter a viewer already knows the
 * answer to before they open the site.
 *
 * A ceiling rather than a range: nobody with two hours free wants a floor under
 * them, and one number is one decision. The steps are the runtime bands the
 * cards already use, so "up to two hours" here and "standard" on a title page
 * are the same boundary rather than two opinions about the same film.
 */
const RUNTIMES = ["90", "120", "150"];
/**
 * The empty one is "let the catalog decide", and it is the default.
 *
 * Searching asks a question, and the best answer is the closest match - which
 * the index computes and the catalog used to throw away, re-sorting by score so
 * that searching "batman" answered with a well-rated show that merely mentions
 * him. With nothing typed there is nothing to match against, so it means the
 * best-rated instead. Sending no sort at all is how the server is told to pick.
 */
const SORTS = ["", "score", "score_israeli", "year", "name", "recently_added"];
/**
 * Which way round each sort reads when nobody says, mirroring the API.
 *
 * Kept here so the arrow is right before anybody touches it - a control that
 * shows the wrong direction until you use it is worse than no control.
 */
const NATURAL_ORDER = {
  "": "desc",
  score: "desc",
  score_israeli: "desc",
  year: "desc",
  name: "asc",
  recently_added: "desc",
};
const AVAILABILITY = ["current", "any", "gone"];

// The service selection is remembered here - the same place theme and language
// live - so a viewer's chosen services default back on their next visit.
const STORAGE_SOURCES = "eifo.sources";

function saveSources(keys) {
  try {
    window.localStorage.setItem(STORAGE_SOURCES, JSON.stringify(keys));
  } catch {
    // Private browsing can refuse storage; the filter simply will not persist.
  }
}

/** The remembered service keys, or null if nothing was ever saved. */
function readSavedSources() {
  try {
    const value = JSON.parse(window.localStorage.getItem(STORAGE_SOURCES) ?? "null");
    return Array.isArray(value) ? value.filter((key) => typeof key === "string") : null;
  } catch {
    return null;
  }
}

export function createHomeView({ mount, app, router, items }) {
  return async function render(route) {
    const { t, language, sources, user } = app.get();
    const filters = paramsToFilters(route.search);

    // An explicit ?sources= in the URL wins (shared/deep links); otherwise fall
    // back to the viewer's remembered selection, keeping only services we still
    // track so a retired one never filters the catalog down to nothing.
    if (!new URLSearchParams(route.search).has("sources")) {
      const saved = readSavedSources();
      if (saved) {
        filters.sources = saved.filter((key) =>
          sources.some((s) => s.key === key && s.title_count > 0),
        );
      }
    }

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

    // A genre list is a nicety; failing to load one must not cost the catalog.
    let genres = [];
    try {
      genres = await listGenres();
    } catch {
      genres = [];
    }

    const filterBar = buildFilterBar({
      state,
      sources,
      genres,
      language,
      user,
      t,
      onChange: apply,
    });
    replace(mount, filterBar.node, region);

    let observer = null;
    let requestToken = 0;

    function apply(patch) {
      Object.assign(state.filters, patch);
      state.page = 1;
      state.loaded = [];
      state.done = false;
      // Remember a service change so it becomes the default next time.
      if ("sources" in patch) saveSources([...state.filters.sources]);
      // A change can come from anywhere in the bar - the "my services" preset
      // sets several chips at once - so the whole bar re-reads the state.
      filterBar.sync();
      // Filters live in the URL so a filtered view can be shared or reloaded.
      router.replaceSearch(filtersToParams(state.filters).toString());
      load({ reset: true });
    }

    async function load({ reset = false } = {}) {
      // A new search always goes, even with one in flight. It used to be turned
      // away for exactly as long as a request took, while apply() had already
      // cleared the results, changed the filters and rewritten the URL - so the
      // address bar said one thing, the grid showed another, and nothing was
      // pending to reconcile them. Only scrolling for the next page defers,
      // which is what state.loading is really guarding.
      if (!reset && (state.loading || state.done)) return;

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
        // The catalog does not wait on a personal-data request to appear. The
        // toggles fill in a beat later, and only if there was anything to fill.
        const mine = await loadMineFor(
          page.items.map((title) => title.id),
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
              // Everything, including the folded-away filters: leaving those
              // set is how you end up staring at no results with no visible
              // reason why.
              onAction: () => apply({ ...DEFAULT_FILTERS }),
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
        state.loaded.map((title, index) =>
          // Only for somebody who has somewhere to put them. Signed out, the
          // buttons would be two dead controls on every card in the catalog.
          titleCard(
            title,
            language,
            index,
            user ? cardActions({ titleId: title.id, items, t }) : null,
          ),
        ),
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

    // The header owns the search box; this is the same apply() the filter bar
    // calls, reached from outside.
    const onHeaderQuery = (event) => {
      if (event.detail.q !== state.filters.q) apply({ q: event.detail.q });
    };
    window.addEventListener(QUERY_EVENT, onHeaderQuery);

    observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting) && !state.done && !state.error) {
        state.page += 1;
        load();
      }
    });
    observer.observe(sentinel);

    await load({ reset: true });

    return () => {
      window.removeEventListener(QUERY_EVENT, onHeaderQuery);
      observer?.disconnect();
    };
  };
}

function buildFilterBar({ state, sources, genres, language, user, t, onChange }) {
  const combo = serviceCombo({ state, sources, user, t, onChange });
  const more = moreFilters({ state, genres, language, t, onChange });
  const direction = sortDirection({ state, t, onChange });

  const type = select({
    values: TYPES,
    current: state.filters.type,
    label: (value) => t(`filters.type.${value || "any"}`),
    // Asking for series drops any length: we hold one episode's runtime, not
    // a season's, so the two together could only ever mean an empty grid.
    onChange: (value) =>
      onChange(value === "series" ? { type: value, runtimeMax: "" } : { type: value }),
  });
  const available = select({
    values: AVAILABILITY,
    current: state.filters.available,
    label: (value) => t(`filters.available.${value}`),
    onChange: (value) => onChange({ available: value }),
  });
  const sort = select({
    values: SORTS,
    current: state.filters.sort,
    label: (value) => t(`filters.sort.${value}`),
    // A new field starts the way it reads best; the arrow is for arguing
    // with that, not something to re-argue on every change.
    onChange: (value) => onChange({ sort: value, order: "" }),
  });

  const node = el(
    "div",
    { class: "filters" },
    el("div", { class: "filters__row shell" }, [
      combo.node,
      type,
      available,
      sort,
      direction.node,
      more.node,
      // A way out of the catalog rather than another way through it, so it sits
      // at the end of the row and is a link: what is new is a page, not a
      // filter, and something to come back from.
      el("a", { class: "control control--new", href: "#/new", text: t("filters.whatsNew") }),
    ]),
  );

  return {
    node,
    sync: () => {
      // The selects re-read the state like everything else: a length chip in
      // the panel asks for films, and a row still saying "All" while the grid
      // shows only films is the bar contradicting itself.
      type.value = state.filters.type;
      available.value = state.filters.available;
      sort.value = state.filters.sort;
      combo.sync();
      more.sync();
      direction.sync();
    },
  };
}

/**
 * The arrow beside the sort: read it the other way round.
 *
 * Its own control rather than five more options in the select, which would
 * double the list to say one thing about each entry. Empty means the field's
 * own direction, so a URL only carries it when somebody disagreed.
 */
function sortDirection({ state, t, onChange }) {
  const current = () => state.filters.order || NATURAL_ORDER[state.filters.sort] || "desc";

  const node = el("button", {
    class: "control control--order",
    type: "button",
    onClick: () => {
      const flipped = current() === "desc" ? "asc" : "desc";
      onChange({ order: flipped === NATURAL_ORDER[state.filters.sort] ? "" : flipped });
    },
  });

  function sync() {
    // Nothing to reverse: "best match" has one direction, and which way round
    // it runs is the server's business rather than a question to put to anyone.
    node.hidden = !state.filters.sort;

    const descending = current() === "desc";
    node.textContent = descending ? "↓" : "↑";
    node.setAttribute("aria-label", t(descending ? "filters.order.desc" : "filters.order.asc"));
    node.setAttribute("aria-pressed", String(Boolean(state.filters.order)));
    node.title = node.getAttribute("aria-label");
  }

  sync();
  return { node, sync };
}

/**
 * Year, genre and minimum score - the filters that answer "80s films" and
 * "something well reviewed", which the API has always accepted and the client
 * has never sent.
 *
 * Folded away behind a disclosure because they are the second question, not the
 * first: the bar stays as short as it was for anybody who does not want them.
 */
function moreFilters({ state, genres, language, t, onChange }) {
  const years = yearRange({ state, t, onChange });
  const length = runtimeChips({ state, t, onChange });
  const chosenGenres = () => new Set(state.filters.genres);
  const boxes = new Map();

  const genreOptions = genres.map((genre) => {
    const id = String(genre.id);
    const box = el("input", {
      type: "checkbox",
      class: "combo__check",
      checked: chosenGenres().has(id) || undefined,
      onChange: () => {
        const next = chosenGenres();
        if (box.checked) next.add(id);
        else next.delete(id);
        onChange({ genres: [...next] });
      },
    });
    boxes.set(id, box);
    return el(
      "li",
      {},
      el("label", { class: "combo__option" }, [
        box,
        el("span", { class: "combo__name", text: genreName(genre, language) }),
      ]),
    );
  });

  const score = select({
    values: SCORES,
    current: state.filters.scoreMin,
    label: (value) => (value ? t("filters.scoreAtLeast", { score: value }) : t("filters.scoreAny")),
    onChange: (value) => onChange({ scoreMin: value }),
  });

  const label = el("span", { class: "combo__label" });
  const node = el("details", { class: "combo combo--more" }, [
    el("summary", { class: "combo__trigger" }, [
      label,
      el("span", { class: "combo__caret", "aria-hidden": "true", text: "▾" }),
    ]),
    el("div", { class: "combo__panel combo__panel--wide" }, [
      // First in the panel: "how long have I got" is the question somebody
      // opens the site already knowing the answer to.
      el("div", { class: "morefilters__group" }, [
        el("h3", { class: "morefilters__heading", text: t("filters.length") }),
        length.node,
        el("p", { class: "morefilters__note", text: t("filters.lengthNote") }),
      ]),
      el("div", { class: "morefilters__group" }, [
        el("h3", { class: "morefilters__heading", text: t("filters.years") }),
        years.node,
        el("p", { class: "morefilters__note", text: t("filters.yearsNote") }),
      ]),
      genreOptions.length
        ? el("div", { class: "morefilters__group" }, [
            el("h3", { class: "morefilters__heading", text: t("filters.genres") }),
            el("ul", { class: "combo__list" }, genreOptions),
          ])
        : null,
      el("div", { class: "morefilters__group" }, [
        el("h3", { class: "morefilters__heading", text: t("filters.score") }),
        score,
        el("p", { class: "morefilters__note", text: t("filters.scoreNote") }),
      ]),
    ]),
  ]);

  function sync() {
    const active = chosenGenres();
    for (const [id, box] of boxes) box.checked = active.has(id);
    score.value = state.filters.scoreMin;
    years.sync();
    length.sync();

    const count = countActive(state.filters);
    label.textContent = count ? t("filters.moreSome", { count }) : t("filters.more");
    node.classList.toggle("combo--active", count > 0);
  }

  sync();
  return { node, sync };
}

/** How many of the folded-away filters are doing something. */
function countActive(filters) {
  return (
    (filters.runtimeMax ? 1 : 0) +
    (filters.yearMin || filters.yearMax ? 1 : 0) +
    filters.genres.length +
    (filters.scoreMin ? 1 : 0)
  );
}

/**
 * "I have this long free": one row of ceilings, at most one of them on.
 *
 * A second press on the chip that is already on clears it, so the way out is
 * the way in - there is no "any length" chip to hunt for. Choosing a length
 * also asks for films, because a length is a claim we can only make about a
 * film; leaving the type alone would have quietly meant the same thing while
 * looking like it did not.
 */
function runtimeChips({ state, t, onChange }) {
  const chips = RUNTIMES.map((minutes) =>
    el("button", {
      class: "chip",
      type: "button",
      "aria-pressed": "false",
      text: t(`filters.runtime.${minutes}`),
      onClick: () =>
        onChange(
          state.filters.runtimeMax === minutes
            ? { runtimeMax: "" }
            : { runtimeMax: minutes, type: "movie" },
        ),
    }),
  );

  const node = el("div", { class: "morefilters__chips" }, chips);

  function sync() {
    RUNTIMES.forEach((minutes, index) => {
      // aria-pressed is both the state and the styling hook, as the decade
      // shortcuts beneath already use it.
      chips[index].setAttribute("aria-pressed", String(state.filters.runtimeMax === minutes));
    });
  }

  sync();
  return { node, sync };
}

/** A genre in the reader's language, falling back rather than showing nothing. */
function genreName(genre, language) {
  return language === "he" ? genre.name_he || genre.name_en : genre.name_en || genre.name_he;
}

/**
 * Two year boxes and a row of decade shortcuts.
 *
 * The boxes are the state; a decade just fills them, and lights up while they
 * still say what it set. A title whose year nobody knows is left out of a year
 * filter, which is what somebody asking for the eighties means.
 */
function yearRange({ state, t, onChange }) {
  const box = (which, placeholder) =>
    el("input", {
      type: "number",
      class: "control control--year",
      inputmode: "numeric",
      min: "1880",
      max: "2100",
      placeholder,
      "aria-label": t(`filters.year.${which}`),
      value: state.filters[which] || "",
      onChange: (event) => onChange({ [which]: event.currentTarget.value.trim() }),
    });

  const from = box("yearMin", t("filters.year.fromShort"));
  const to = box("yearMax", t("filters.year.toShort"));

  const chips = DECADES.map((decade) =>
    el("button", {
      class: "chip",
      type: "button",
      "aria-pressed": "false",
      text: t(`filters.decade.${decade.key}`),
      onClick: () =>
        onChange(
          isDecade(state.filters, decade)
            ? { yearMin: "", yearMax: "" }
            : { yearMin: String(decade.min), yearMax: String(decade.max) },
        ),
    }),
  );

  const node = el("div", { class: "morefilters__years" }, [
    el("div", { class: "morefilters__boxes" }, [from, el("span", { text: "–" }), to]),
    el("div", { class: "morefilters__chips" }, chips),
  ]);

  function sync() {
    from.value = state.filters.yearMin || "";
    to.value = state.filters.yearMax || "";
    DECADES.forEach((decade, index) => {
      // aria-pressed is both the state and the styling hook, exactly as the
      // service chips in settings already use it.
      chips[index].setAttribute("aria-pressed", String(isDecade(state.filters, decade)));
    });
  }

  sync();
  return { node, sync };
}

function isDecade(filters, decade) {
  return filters.yearMin === String(decade.min) && filters.yearMax === String(decade.max);
}

/**
 * The services multi-select: a dropdown of every service, each with its colour
 * dot and a checkbox, plus select-all / clear (and "my services" when signed in).
 *
 * Built on <details> so it opens, closes and takes keyboard focus with no JS of
 * its own; the checkboxes are native for the same reason.
 */
function serviceCombo({ state, sources, user, t, onChange }) {
  const chosen = () => new Set(state.filters.sources);
  const boxes = new Map();

  // Only services with titles right now - a retired source with an empty
  // catalog is nothing to filter by, so it does not belong in the list.
  const shown = sources.filter((source) => source.title_count > 0);

  const options = shown.map((source) => {
    const box = el("input", {
      type: "checkbox",
      class: "combo__check",
      checked: chosen().has(source.key) || undefined,
      onChange: () => {
        const next = chosen();
        if (box.checked) next.add(source.key);
        else next.delete(source.key);
        onChange({ sources: [...next] });
      },
    });
    boxes.set(source.key, box);

    return el(
      "li",
      {},
      el(
        "label",
        { class: "combo__option", style: { "--source-color": sourceColorVar(source.key) } },
        [
          box,
          el("span", { class: "combo__dot", "aria-hidden": "true" }),
          el("span", { class: "combo__name", text: source.name }),
          source.title_count
            ? el("span", { class: "combo__count", text: String(source.title_count) })
            : null,
        ],
      ),
    );
  });

  const stack = el("span", { class: "combo__stack", "aria-hidden": "true" });
  const label = el("span", { class: "combo__label" });
  const trigger = el("summary", { class: "combo__trigger", "aria-label": t("filters.services") }, [
    stack,
    label,
    el("span", { class: "combo__caret", "aria-hidden": "true", text: "▾" }),
  ]);

  const mine = myServicesAction({ user, sources, state, t, onChange });
  const actions = el("div", { class: "combo__actions" }, [
    el("button", {
      class: "combo__action",
      type: "button",
      text: t("filters.selectAll"),
      onClick: () => onChange({ sources: shown.map((s) => s.key) }),
    }),
    el("button", {
      class: "combo__action combo__action--quiet",
      type: "button",
      text: t("filters.clear"),
      onClick: () => onChange({ sources: [] }),
    }),
    mine,
  ]);

  const node = el("details", { class: "combo" }, [
    trigger,
    el("div", { class: "combo__panel" }, [actions, el("ul", { class: "combo__list" }, options)]),
  ]);

  function sync() {
    const active = chosen();
    for (const [key, box] of boxes) box.checked = active.has(key);

    label.textContent = active.size
      ? t("filters.servicesSome", { count: active.size })
      : t("filters.servicesAll");
    node.classList.toggle("combo--active", active.size > 0);

    // One dot per selected service, side by side; hover a dot for its name.
    replace(
      stack,
      shown
        .filter((s) => active.has(s.key))
        .map((s) =>
          el("span", {
            class: "combo__stackdot",
            title: s.name,
            style: { "--source-color": sourceColorVar(s.key) },
          }),
        ),
    );

    if (mine && mine.dataset.mine) {
      mine.classList.toggle("is-on", presetApplied({ user, sources, state }));
    }
  }

  sync();
  return { node, sync };
}

/** The "my services" preset, folded into the combo's action row when signed in. */
function myServicesAction({ user, sources, state, t, onChange }) {
  if (!user) return null;

  const preset = presetKeys(user, sources);
  if (!preset.length) {
    return el("a", {
      class: "combo__action combo__action--hint",
      href: "#/settings",
      text: t("filters.myServicesEmpty"),
    });
  }

  const button = el("button", {
    class: "combo__action combo__action--mine",
    type: "button",
    "data-mine": "1",
    text: t("filters.myServices"),
    onClick: () =>
      onChange({ sources: presetApplied({ user, sources, state }) ? [] : preset }),
  });
  return button;
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
