/* `#/for-you` - every title picked for the member, to sort, narrow and scroll.
 *
 * The row above the catalog is a dozen picks arranged for variety. This is
 * the list it is chosen from, best match first, in the catalog's own grid and
 * under the catalog's own filter bar - less the controls that mean nothing
 * for a pick.
 */

import { ApiError, listGenres, listRecommendations, patchMe } from "../api.js";
import { cardActions, loadMineFor } from "../account.js";
import { DEFAULT_FILTERS } from "../format.js";
import { PICK_SORTS, cardWithReason, forYouReason, picksFilters, picksSearch } from "../picks.js";
import { el, replace, skeletonCards, stateBlock } from "../ui.js";
import { buildFilterBar, presetKeys, withOtherServices } from "./home.js";

const PAGE_SIZE = 24;

export function createForYouView({ mount, app, router, items }) {
  return async function render(route) {
    const { t, language, user, userAddedCount } = app.get();

    if (!user) {
      replace(
        mount,
        el(
          "div",
          { class: "shell" },
          stateBlock({
            title: t("forYou.signIn"),
            actionLabel: t("mylist.browse"),
            onAction: () => router.navigate("home"),
          }),
        ),
      );
      return null;
    }

    const sources = withOtherServices(app.get().sources, { count: userAddedCount, t });
    // The member's services as they are now: editing them in the bar changes
    // what an address with no services in it means.
    const mine = () => presetKeys(app.get().user, sources);

    const state = {
      filters: picksFilters(route.search, mine()),
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

    // A genre list is a nicety; failing to load one must not cost the picks.
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
      onSaveMine: saveMine,
      picks: {
        sorts: PICK_SORTS,
        sortLabel: (value) => t(value ? `filters.sort.${value}` : "forYou.sort.match"),
      },
    });
    replace(
      mount,
      el("div", { class: "foryou__head shell" }, [
        el("h1", { class: "page__title", text: t("forYou.title") }),
        el("p", { class: "foryou__note", text: t("forYou.pageNote") }),
      ]),
      filterBar.node,
      region,
    );

    let requestToken = 0;

    function apply(patch) {
      Object.assign(state.filters, patch);
      state.page = 1;
      state.loaded = [];
      state.done = false;
      filterBar.sync();
      // Filters live in the URL so a narrowed list survives a visit to a title.
      router.replaceSearch(picksSearch(state.filters, mine()));
      load({ reset: true });
    }

    async function saveMine(ids) {
      const saved = await patchMe({ my_source_ids: ids });
      app.set({ user: saved });
      return saved;
    }

    async function load({ reset = false } = {}) {
      // As in the catalog: a new question always goes; only the next page waits.
      if (!reset && (state.loading || state.done)) return;

      state.loading = true;
      state.error = null;
      const token = ++requestToken;

      if (reset) {
        replace(grid, skeletonCards());
        status.textContent = t("results.searching");
      }

      try {
        const page = await listRecommendations(state.filters, {
          page: state.page,
          pageSize: PAGE_SIZE,
        });
        // A slower earlier request must not overwrite a newer result.
        if (token !== requestToken) return;

        state.total = page.total;
        state.loaded = reset ? page.items : [...state.loaded, ...page.items];
        state.done = state.loaded.length >= page.total || page.items.length === 0;
        paint();
        const held = await loadMineFor(
          page.items.map((pick) => pick.id),
          { user, items },
        );
        if (held && token === requestToken) paint();
      } catch (error) {
        if (token !== requestToken) return;
        state.error = error;
        paintError();
      } finally {
        if (token === requestToken) state.loading = false;
      }
    }

    /** Whether anything but the order is narrowing the list. */
    function narrowed() {
      const { sources: on, type, genres: of, scoreMin, runtimeMax } = state.filters;
      return Boolean(on.length || type || of.length || scoreMin || runtimeMax);
    }

    function paint() {
      if (!state.loaded.length) {
        status.textContent = "";
        // Two different nothings: nothing fits what was asked for, which
        // clearing the filters answers, and nothing to go on yet, which only
        // rating something does.
        const block = narrowed()
          ? stateBlock({
              title: t("empty.title"),
              body: t("empty.body"),
              actionLabel: t("empty.clear"),
              onAction: () => apply({ ...DEFAULT_FILTERS }),
            })
          : stateBlock({
              title: t("forYou.nothingTitle"),
              body: t("forYou.empty"),
              actionLabel: t("mylist.browse"),
              onAction: () => router.navigate("home"),
            });
        replace(grid, el("li", { style: { "grid-column": "1 / -1" } }, block));
        return;
      }

      replace(
        status,
        el("span", { class: "results__count", text: String(state.total) }),
        el("span", { text: t("results.countLabel") }),
      );
      replace(
        grid,
        state.loaded.map((pick, index) =>
          cardWithReason(pick, language, index, t, forYouReason(pick, language, t), (titleId) =>
            cardActions({ titleId, items, t }),
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
