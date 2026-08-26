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
  replace(panel, [statStrip(stats, { t, language }), sourcesTable(sources, { t, language })]);
  return null;
}

function statStrip(stats, { t, language }) {
  const stale = stats.sources_stale > 0;
  const lastRun = stats.last_run_at
    ? formatDate(stats.last_run_at, language)
    : t("manage.stat.never");

  const tiles = [
    { label: t("manage.stat.titles"), value: stats.title_count },
    { label: t("manage.stat.scored"), value: stats.titles_with_score },
    {
      label: t("manage.stat.missingPoster"),
      value: stats.titles_missing_poster,
    },
    { label: t("manage.stat.people"), value: stats.people_count },
    { label: t("manage.stat.available"), value: stats.titles_available },
    { label: t("manage.stat.offers"), value: stats.current_offers },
    {
      label: t("manage.stat.pending"),
      value: stats.pending_reviews,
      // A queue with anything in it is content missing from the catalog, so it
      // is never merely informational.
      warn: stats.pending_reviews > 0,
    },
    {
      label: t("manage.stat.stale"),
      value: t("manage.staleOf", {
        stale: stats.sources_stale,
        total: stats.sources_total,
      }),
      warn: stale,
    },
    {
      label: t("manage.stat.lastRun"),
      value: lastRun,
      warn: !stats.last_run_at,
    },
  ];

  return el(
    "ul",
    { class: "stats" },
    tiles.map((tile) =>
      // A tile holding a date or a ratio is not a headline number, and set at
      // the same size it wraps to three lines and stretches every tile in the
      // row to match it.
      el(
        "li",
        {
          class: [
            "stat",
            tile.warn ? "stat--warn" : "",
            typeof tile.value === "number" ? "" : "stat--text",
          ]
            .filter(Boolean)
            .join(" "),
        },
        [
          el("span", { class: "stat__value", text: String(tile.value) }),
          el("span", { class: "stat__label", text: tile.label }),
        ],
      ),
    ),
  );
}

function sourcesTable(sources, { t, language }) {
  const widest = Math.max(1, ...sources.map((source) => source.title_count));

  return el("section", { class: "panel" }, [
    el("h2", { class: "panel__title", text: t("manage.sources") }),
    el(
      "ul",
      { class: "sources" },
      sources.map((source) => sourceRow(source, { t, language, widest })),
    ),
  ]);
}

function sourceRow(source, { t, language, widest }) {
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
    source.stale
      ? el("span", {
          class: "badge badge--warn",
          text: t("manage.source.stale"),
        })
      : null,
    source.active ? null : el("span", { class: "badge", text: t("manage.source.retired") }),
    source.pending_reviews
      ? el("span", {
          class: "badge badge--warn",
          text: `${t("manage.source.pending")}: ${source.pending_reviews}`,
        })
      : null,
  ].filter(Boolean);

  return el("li", { class: "source" }, [
    el("div", { class: "source__main" }, [
      el("span", { class: "source__name", text: source.name }),
      el("span", { class: "source__key", text: source.key, dir: "ltr" }),
      ...badges,
    ]),
    el("div", { class: "source__coverage" }, [
      el("span", {
        class: "source__bar",
        style: {
          "--fill": `${Math.round((source.title_count / widest) * 100)}%`,
        },
        "aria-hidden": "true",
      }),
      el("span", { class: "source__count", text: String(source.title_count) }),
    ]),
    el("span", {
      class: "source__synced",
      text: source.last_sync_at
        ? formatDate(source.last_sync_at, language)
        : t("manage.stat.never"),
    }),
    el("div", { class: "source__switch" }, [toggle, status]),
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
