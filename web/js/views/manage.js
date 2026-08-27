/* `#/manage` - the operator's tab: is the catalog alright, and if not, why.
 *
 * Three panels behind one route. Overview answers "is anything wrong" in the
 * time it takes to look at it; runs answers "what happened last night"; the
 * review queue is where the answer to "why is this listing missing" gets acted
 * on, and it lives in its own module because triage is a different job from
 * reading dials.
 *
 * The tab is hidden from anybody the server does not consider an administrator,
 * and every endpoint behind it 404s for them regardless. Both, so neither has
 * to be the only one that is right.
 */

import { getAdminStats, getRun, listAdminSources, listRuns, setSourceEnabled } from "../api.js";
import { formatDate } from "../format.js";
import { el, replace, stateBlock } from "../ui.js";
import { createReviewView } from "./review.js";

const RUNS_PAGE_SIZE = 25;

const TABS = [
  { key: "overview", label: "manage.tab.overview" },
  { key: "runs", label: "manage.tab.runs" },
  { key: "review", label: "manage.tab.review" },
];

export function createManageView({ mount, app, router }) {
  const review = createReviewView({ app, router });

  return async function render(route) {
    const { t, user } = app.get();

    if (!user?.is_admin) {
      // The same answer the API gives: this is not yours. Not an error state -
      // nothing went wrong, it is simply somebody else's page.
      replace(
        mount,
        el(
          "div",
          { class: "shell" },
          stateBlock({
            title: t("manage.denied"),
            body: t("manage.deniedBody"),
            actionLabel: t("mylist.browse"),
            onAction: () => router.navigate("home"),
          }),
        ),
      );
      return null;
    }

    const params = new URLSearchParams(route.search);
    const tab = TABS.find((entry) => entry.key === params.get("tab")) ?? TABS[0];

    const panel = el("section", { class: "manage__panel" });
    replace(
      mount,
      el("div", { class: "shell" }, [
        el("h1", { class: "page__title", text: t("manage.title") }),
        tabStrip(tab, t, router),
        panel,
      ]),
    );

    if (tab.key === "review") return review.mount(panel, params);
    if (tab.key === "runs") return runsPanel(panel, { t, app, params, router });
    return overviewPanel(panel, { t, app });
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
        onClick: () => router.navigate("manage", [], `tab=${tab.key}`),
      }),
    ),
  );
}

/* -- percentages ----------------------------------------------------------- */

/** Where a completeness figure sits: green above 95, amber above 75, red below. */
export function percentBand(percent) {
  if (percent === null || percent === undefined) return "none";
  if (percent > 95) return "good";
  if (percent > 75) return "warn";
  return "bad";
}

/**
 * A share of a whole, or null when there is no whole to take a share of.
 *
 * A source with nothing in it has no completeness - not 0%, which would read
 * as "everything is missing" and colour the row red for a service that has
 * simply never synced.
 */
export function share(part, whole) {
  return whole > 0 ? (part / whole) * 100 : null;
}

/**
 * A percentage as somebody reads it.
 *
 * Whole numbers, because a dashboard is read at a glance - except below ten,
 * where rounding 0.7% to 1% triples it and rounding it to 0% hides a queue
 * that is not empty.
 *
 * Neither end is allowed to round to its bound. A source with three listings
 * still queued is 99.91% cleared, and a column reading "100%" over a queue that
 * is not empty is the one number on this page nobody would think to check.
 */
export function formatPercent(percent) {
  if (percent === null || percent === undefined) return "—";
  if (percent >= 100) return "100%";
  if (percent > 99) return "99%";
  if (percent <= 0) return "0%";
  if (percent < 0.1) return "<0.1%";
  return percent < 10 ? `${percent.toFixed(1)}%` : `${Math.round(percent)}%`;
}

/* -- overview -------------------------------------------------------------- */

async function overviewPanel(panel, { t, app }) {
  replace(panel, el("p", { class: "muted", text: t("results.searching") }));

  let stats;
  let sources;
  try {
    [stats, sources] = await Promise.all([getAdminStats(), listAdminSources()]);
  } catch (error) {
    replace(
      panel,
      stateBlock({
        title: t("error.title"),
        body: error?.detail || t("error.body"),
      }),
    );
    return null;
  }

  const { language } = app.get();
  replace(panel, [statStrip(stats, { t }), sourcesTable(sources, { t, language })]);
  return null;
}

function statStrip(stats, { t }) {
  const stale = stats.sources_stale > 0;

  // Three of these are completeness rather than counts, and completeness is
  // what the colours are for. Each is stated in the direction where more is
  // better - a catalog 3% short of posters is 97% covered, and colouring the
  // 3 would paint a healthy figure red.
  const withPoster = stats.title_count - stats.titles_missing_poster;
  const cleared = stats.reviews_total - stats.pending_reviews;

  const tiles = [
    { label: t("manage.stat.titles"), value: stats.title_count },
    {
      label: t("manage.stat.scored"),
      percent: share(stats.titles_with_score, stats.title_count),
      count: t("manage.stat.ofTitles", {
        count: stats.titles_with_score,
        total: stats.title_count,
      }),
    },
    {
      label: t("manage.stat.withPoster"),
      percent: share(withPoster, stats.title_count),
      count: t("manage.stat.missing", { count: stats.titles_missing_poster }),
    },
    {
      label: t("manage.stat.queueCleared"),
      percent: share(cleared, stats.reviews_total),
      count: t("manage.stat.waiting", { count: stats.pending_reviews }),
    },
    { label: t("manage.stat.people"), value: stats.people_count },
    { label: t("manage.stat.available"), value: stats.titles_available },
    { label: t("manage.stat.offers"), value: stats.current_offers },
    {
      label: t("manage.stat.stale"),
      value: t("manage.staleOf", {
        stale: stats.sources_stale,
        total: stats.sources_total,
      }),
      warn: stale,
    },
  ];

  return el(
    "ul",
    { class: "stats" },
    tiles.map((tile) => statTile(tile)),
  );
}

function statTile(tile) {
  const isPercent = tile.percent !== undefined;
  // A tile holding a date or a ratio is not a headline number, and set at the
  // same size it wraps to three lines and stretches every tile in the row.
  const asText = !isPercent && typeof tile.value !== "number";

  return el(
    "li",
    {
      class: ["stat", tile.warn ? "stat--warn" : "", asText ? "stat--text" : ""]
        .filter(Boolean)
        .join(" "),
    },
    [
      el("span", {
        class: isPercent
          ? `stat__value stat__value--${percentBand(tile.percent)}`
          : "stat__value",
        text: isPercent ? formatPercent(tile.percent) : String(tile.value),
      }),
      el("span", { class: "stat__label", text: tile.label }),
      // The figure the percentage was taken from. A share with no numbers
      // behind it is a number nobody can check.
      tile.count ? el("span", { class: "stat__count", text: tile.count }) : null,
    ],
  );
}

function sourcesTable(sources, { t, language }) {
  const columns = [
    t("manage.col.source"),
    t("manage.col.titles"),
    t("manage.col.poster"),
    t("manage.col.score"),
    t("manage.col.queue"),
    t("manage.col.enriched"),
    t("manage.source.lastSync"),
    t("manage.source.enabled"),
  ];

  return el("section", { class: "panel" }, [
    el("h2", { class: "panel__title", text: t("manage.sources") }),
    // Its own scroller: eight columns do not fit a phone, and a table that
    // widens the page makes every other panel scroll sideways with it.
    el(
      "div",
      { class: "table-scroll" },
      el("table", { class: "sources" }, [
        el(
          "thead",
          {},
          el(
            "tr",
            {},
            columns.map((name, index) =>
              el("th", { scope: "col", class: index === 0 ? "" : "num", text: name }),
            ),
          ),
        ),
        el(
          "tbody",
          {},
          sources.map((source) => sourceRow(source, { t, language })),
        ),
      ]),
    ),
  ]);
}

/** A completeness cell: the share, coloured, with its own numerator beneath. */
function percentCell(part, whole, { title = "" } = {}) {
  const percent = share(part, whole);
  return el("td", { class: "num", title }, [
    el("span", {
      class: `pct pct--${percentBand(percent)}`,
      text: formatPercent(percent),
    }),
    percent === null ? null : el("span", { class: "pct__of", text: String(part) }),
  ]);
}

function sourceRow(source, { t, language }) {
  // Three things can decide this - an operator, the config file, the plugin's
  // own default - and the row can honestly distinguish only the first from the
  // other two. It used to claim "from the config file" about sources the file
  // had never heard of.
  const status = el("span", {
    class: "source__note",
    text: source.enabled === null ? t("manage.source.fromConfig") : t("manage.source.override"),
  });

  const toggle = el("input", {
    class: "switch",
    type: "checkbox",
    role: "switch",
    checked: source.effective_enabled,
    "aria-label": `${source.name}: ${t("manage.source.enabled")}`,
  });

  toggle.addEventListener("change", async (event) => {
    const input = event.currentTarget;
    input.disabled = true;
    try {
      const updated = await setSourceEnabled(source.key, input.checked);
      // Believe the server, not the checkbox: it folds the config file and the
      // override together and the answer can differ from what was clicked.
      input.checked = updated.effective_enabled;
      status.textContent =
        updated.enabled === null ? t("manage.source.fromConfig") : t("manage.source.override");
    } catch {
      input.checked = !input.checked;
      status.textContent = t("error.title");
    } finally {
      input.disabled = false;
    }
  });

  const badges = [
    source.stale ? el("span", { class: "badge badge--warn", text: t("manage.source.stale") }) : null,
    source.active ? null : el("span", { class: "badge", text: t("manage.source.retired") }),
  ].filter(Boolean);

  // A parked listing is not one of this source's titles yet - it is a listing
  // waiting to become one - so the share is of everything it has offered us.
  const seen = source.title_count + source.pending_reviews;

  return el("tr", {}, [
    el("td", {}, [
      el("span", { class: "source__name", text: source.name }),
      el("span", { class: "source__key", text: source.key, dir: "ltr" }),
      ...badges,
    ]),
    el("td", { class: "num source__count", text: String(source.title_count) }),
    percentCell(source.titles_with_poster, source.title_count),
    percentCell(source.titles_with_score, source.title_count),
    // Cleared rather than waiting, so more is better here as it is everywhere
    // else in the row and one colour scale reads the same across all of them.
    percentCell(source.title_count, seen, {
      title: t("manage.source.waitingCount", { count: source.pending_reviews }),
    }),
    percentCell(source.titles_enriched, source.title_count),
    el("td", {
      class: "num source__synced",
      text: source.last_sync_at ? formatDate(source.last_sync_at, language) : t("manage.stat.never"),
    }),
    el("td", { class: "num" }, el("div", { class: "source__switch" }, [toggle, status])),
  ]);
}

/* -- runs ------------------------------------------------------------------ */

async function runsPanel(panel, { t, app, params, router }) {
  const source = params.get("source") ?? "";
  const status = params.get("status") ?? "";

  replace(panel, el("p", { class: "muted", text: t("results.searching") }));

  let page;
  try {
    page = await listRuns({ source, status }, { pageSize: RUNS_PAGE_SIZE });
  } catch (error) {
    replace(
      panel,
      stateBlock({
        title: t("error.title"),
        body: error?.detail || t("error.body"),
      }),
    );
    return null;
  }

  if (!page.items.length) {
    replace(panel, stateBlock({ title: t("manage.runs.empty") }));
    return null;
  }

  const { language } = app.get();
  replace(panel, [
    runFilters({ t, source, status, router }),
    el(
      "ul",
      { class: "runs" },
      page.items.map((run) => runRow(run, { t, language })),
    ),
  ]);
  return null;
}

function runFilters({ t, source, status, router }) {
  const go = (next) => {
    const params = new URLSearchParams({ tab: "runs" });
    if (next.source) params.set("source", next.source);
    if (next.status) params.set("status", next.status);
    router.navigate("manage", [], params.toString());
  };

  const statuses = ["", "ok", "failed", "crashed", "aborted_suspicious"];
  return el(
    "div",
    { class: "chips" },
    statuses.map((value) =>
      el("button", {
        class: "chip",
        type: "button",
        "aria-pressed": String(value === status),
        text: value ? value.replace(/_/g, " ") : t("manage.runs.all"),
        onClick: () => go({ source, status: value }),
      }),
    ),
  );
}

function runRow(run, { t, language }) {
  const body = el("div", { class: "run__log-slot" });
  const toggle = run.has_log
    ? el("button", {
        class: "button button--quiet",
        type: "button",
        text: t("manage.runs.showLog"),
        "aria-expanded": "false",
        onClick: (event) => toggleLog(event.currentTarget, body, run.id, t),
      })
    : el("span", { class: "muted", text: t("manage.runs.noLog") });

  return el("li", { class: `run run--${run.status}` }, [
    el("div", { class: "run__head" }, [
      el("span", { class: "run__phase", text: run.phase }),
      el("span", {
        class: "run__source",
        text: run.source_key ?? "-",
        dir: "ltr",
      }),
      el("span", {
        class: `badge badge--${run.status}`,
        text: run.status.replace(/_/g, " "),
      }),
      el("span", {
        class: "run__when",
        text: formatDate(run.started_at, language),
      }),
      el("span", {
        class: "run__took",
        text: run.duration_seconds == null ? "—" : `${Math.round(run.duration_seconds)}s`,
      }),
      toggle,
    ]),
    statLine(run.stats),
    body,
  ]);
}

/**
 * A run's own tally, flattened into label/value pairs.
 *
 * A tally is not flat. `by_enricher` and `matched_by` are maps - which
 * enricher wrote what, how each title was matched - and both were rendering as
 * `[object Object]`, which is the one thing a number panel must never say.
 * They flatten one level, so "by enricher · tmdb" is its own figure.
 *
 * `errors` is a list, and was being dropped entirely. It becomes a count: the
 * strings themselves belong in the log, which is a click away, and a stat line
 * that swallows the word "errors" is worse than one that is merely terse.
 *
 * Empty maps and empty lists are left out. A clean run should read as a clean
 * run, not as a row of zeroes.
 *
 * Exported for its own tests; nothing else calls it.
 */
export function statEntries(stats) {
  const entries = [];

  for (const [key, value] of Object.entries(stats ?? {})) {
    const label = key.replace(/_/g, " ");

    if (value === null || value === undefined) continue;

    if (Array.isArray(value)) {
      // Some tallies already record the count beside the list - the sync and
      // enrich ones pair `errors` with `error_count`, singular. Saying
      // "errors 2" next to "error count 2" is noise, so the singular form is
      // checked as well as the plain one.
      const counted = `${key}_count` in stats || `${key.replace(/s$/, "")}_count` in stats;
      if (value.length && !counted) entries.push([label, String(value.length)]);
      continue;
    }

    if (typeof value === "object") {
      for (const [inner, count] of Object.entries(value)) {
        entries.push([`${label} · ${inner}`, String(count)]);
      }
      continue;
    }

    entries.push([label, String(value)]);
  }

  return entries;
}

function statLine(stats) {
  const entries = statEntries(stats);
  if (!entries.length) return null;

  return el(
    "ul",
    { class: "run__stats" },
    entries.map(([label, value]) =>
      el("li", {}, [
        el("span", { class: "run__stat-key", text: label }),
        el("span", { class: "run__stat-value", text: value }),
      ]),
    ),
  );
}

async function toggleLog(button, slot, runId, t) {
  if (slot.firstChild) {
    replace(slot);
    button.textContent = t("manage.runs.showLog");
    button.setAttribute("aria-expanded", "false");
    return;
  }

  button.disabled = true;
  try {
    const detail = await getRun(runId);
    replace(
      slot,
      // Always ltr: a log is timestamps, logger names and source keys, and in
      // an RTL page the browser would otherwise reorder every line that starts
      // with one and ends with a Hebrew title.
      el("pre", {
        class: "log",
        dir: "ltr",
        tabindex: "0",
        text: detail.log ?? "",
      }),
    );
    button.textContent = t("manage.runs.hideLog");
    button.setAttribute("aria-expanded", "true");
  } catch (error) {
    replace(slot, el("p", { class: "muted", text: error?.detail || t("error.body") }));
  } finally {
    button.disabled = false;
  }
}
