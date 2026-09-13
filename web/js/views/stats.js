/* `#/stats` - how the catalog has grown, and what each service holds now.
 *
 * Two tabs behind one route, the way Manage is laid out, but for everybody:
 * nothing here is operator material, only counts a curious viewer could have
 * worked out by paging through the grid.
 *
 * Growth reads what every sync added off the tally the run already wrote, as a
 * table or as a small chart per service. One chart per service rather than one
 * stacked chart: there are more services than colours anybody can tell apart,
 * and Apple's store alone would flatten every other bar to a hairline.
 *
 * Everything the view is showing lives in the URL, so a chart can be linked to.
 */

import { ApiError, getGrowth, getServiceStats } from "../api.js";
import { formatDate, sourceColorVar } from "../format.js";
import { el, replace, stateBlock } from "../ui.js";
import { formatPercent, percentBand, share } from "./manage.js";

const TABS = ["growth", "services"];
const VIEWS = ["table", "graph"];
const METRICS = ["offers", "titles"];
const RANGES = [30, 90, 365, 3650];
const DEFAULT_RANGE = 90;

/* How many individual syncs the table lists under the per-service summary. */
const RECENT_RUNS = 50;

const DAY_MS = 24 * 60 * 60 * 1000;

/** The view's state, read from the query string, with anything unknown defaulted. */
export function readStatsState(search = "") {
  const params = new URLSearchParams(search);
  const pick = (value, allowed, fallback) => (allowed.includes(value) ? value : fallback);
  const range = Number(params.get("range"));
  return {
    tab: pick(params.get("tab"), TABS, TABS[0]),
    view: pick(params.get("view"), VIEWS, VIEWS[0]),
    metric: pick(params.get("metric"), METRICS, METRICS[0]),
    range: RANGES.includes(range) ? range : DEFAULT_RANGE,
    loads: params.get("loads") === "1",
  };
}

/** The query string for a state, leaving every default out. */
export function writeStatsState(state) {
  const params = new URLSearchParams();
  if (state.tab !== TABS[0]) params.set("tab", state.tab);
  if (state.view !== VIEWS[0]) params.set("view", state.view);
  if (state.metric !== METRICS[0]) params.set("metric", state.metric);
  if (state.range !== DEFAULT_RANGE) params.set("range", String(state.range));
  if (state.loads) params.set("loads", "1");
  return params.toString();
}

/** The figure a metric reads off one sync. */
export function metricOf(run, metric) {
  return metric === "titles" ? run.titles_created : run.offers_added;
}

/**
 * How wide a bar is, for a range.
 *
 * Days for a month, weeks up to a year, months beyond: roughly thirty to sixty
 * bars whatever the range, which is what fits a small chart without every bar
 * becoming a hairline.
 */
export function bucketUnit(days) {
  if (days <= 30) return "day";
  if (days <= 365) return "week";
  return "month";
}

/** The start of the bucket a moment falls in, as a UTC timestamp. */
export function bucketStart(value, unit) {
  const date = new Date(value);
  const day = Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate());
  if (unit === "day") return day;
  if (unit === "month") return Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), 1);
  // Weeks start on Sunday, which is when the Israeli week does.
  return day - date.getUTCDay() * DAY_MS;
}

/** The next bucket's start after one. */
function nextBucket(start, unit) {
  if (unit === "day") return start + DAY_MS;
  if (unit === "week") return start + 7 * DAY_MS;
  const date = new Date(start);
  return Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 1);
}

/**
 * Every bucket from the first one needed to the one holding `now`.
 *
 * A range of "all" starts at the earliest sync rather than ten years back, so
 * the chart is not nine years of empty axis and one year of data.
 */
export function bucketsFor(runs, { days, now = Date.now() }) {
  let from = now - days * DAY_MS;
  if (days >= RANGES[RANGES.length - 1] && runs.length) {
    from = Math.max(from, Math.min(...runs.map((run) => Date.parse(run.started_at))));
  }
  // Sized by the span actually drawn, so six weeks of history under "all" is
  // six weekly bars rather than two monthly ones.
  const unit = bucketUnit(Math.ceil((now - from) / DAY_MS));

  const starts = [];
  for (let at = bucketStart(from, unit); at <= now; at = nextBucket(at, unit)) starts.push(at);
  return { unit, starts };
}

/** The syncs a view counts: all of them, or all but the ones that loaded a catalog. */
export function countedRuns(runs, { loads }) {
  return loads ? runs : runs.filter((run) => !run.catalog_load);
}

/**
 * One row per service: how many syncs, and what they added in total.
 *
 * Ordered by what was added to the service, most first - the table's question
 * is "where did things turn up", and the answer is the top of it.
 */
export function summarizeGrowth(runs) {
  const rows = new Map();
  for (const run of runs) {
    const row = rows.get(run.source_key) ?? {
      source_key: run.source_key,
      source_name: run.source_name,
      syncs: 0,
      offers_added: 0,
      titles_created: 0,
      offers_retired: 0,
      last_sync: null,
    };
    row.syncs += 1;
    row.offers_added += run.offers_added;
    row.titles_created += run.titles_created;
    row.offers_retired += run.offers_retired;
    if (!row.last_sync || run.started_at > row.last_sync) row.last_sync = run.started_at;
    rows.set(run.source_key, row);
  }
  return [...rows.values()].sort(
    (a, b) =>
      b.offers_added - a.offers_added ||
      b.titles_created - a.titles_created ||
      a.source_name.localeCompare(b.source_name),
  );
}

/**
 * Each service's figures laid into the buckets, largest total first.
 *
 * Every service gets every bucket, zeroes included, so the charts share one
 * time axis and a gap reads as a quiet week rather than a missing one.
 */
export function growthSeries(runs, { metric, unit, starts }) {
  const index = new Map(starts.map((start, position) => [start, position]));
  const series = new Map();

  for (const run of runs) {
    const position = index.get(bucketStart(run.started_at, unit));
    if (position === undefined) continue;
    const entry = series.get(run.source_key) ?? {
      source_key: run.source_key,
      source_name: run.source_name,
      values: new Array(starts.length).fill(0),
      total: 0,
    };
    const value = metricOf(run, metric);
    entry.values[position] += value;
    entry.total += value;
    series.set(run.source_key, entry);
  }

  return [...series.values()].sort(
    (a, b) => b.total - a.total || a.source_name.localeCompare(b.source_name),
  );
}

/** A count with its thousands grouped, the way the language groups them. */
export function formatCount(value, language = "en") {
  return new Intl.NumberFormat(language === "he" ? "he-IL" : "en-US").format(value ?? 0);
}

/* -- the view --------------------------------------------------------------- */

export function createStatsView({ mount, app, router }) {
  return async function render(route) {
    const { t } = app.get();
    const state = readStatsState(route.search);
    const go = (change) => router.navigate("stats", [], writeStatsState({ ...state, ...change }));

    const panel = el("section", { class: "stats-page__panel" });
    replace(
      mount,
      el("div", { class: "shell stats-page" }, [
        el("h1", { class: "page__title", text: t("stats.title") }),
        el(
          "div",
          { class: "tabs", role: "tablist" },
          TABS.map((tab) =>
            el("button", {
              class: `tab${tab === state.tab ? " tab--on" : ""}`,
              type: "button",
              role: "tab",
              "aria-selected": String(tab === state.tab),
              text: t(`stats.tab.${tab}`),
              onClick: () => go({ tab }),
            }),
          ),
        ),
        panel,
      ]),
    );

    if (state.tab === "services") return servicesPanel(panel, { app });
    return growthPanel(panel, { app, state, go });
  };
}

function loading(panel, t) {
  replace(panel, el("p", { class: "muted", text: t("results.searching") }));
}

function failed(panel, error, t, retry) {
  const offline = error instanceof ApiError && error.offline;
  replace(
    panel,
    stateBlock({
      title: t("error.title"),
      body: offline ? t("error.offline") : error?.detail || t("error.body"),
      actionLabel: t("error.retry"),
      onAction: retry,
    }),
  );
}

/* -- growth ----------------------------------------------------------------- */

async function growthPanel(panel, { app, state, go }) {
  const { t, language } = app.get();
  loading(panel, t);

  let runs;
  try {
    runs = await getGrowth({ days: state.range });
  } catch (error) {
    failed(panel, error, t, () => growthPanel(panel, { app, state, go }));
    return null;
  }

  const counted = countedRuns(runs, state);
  const controls = growthControls({ t, state, go });

  if (!counted.length) {
    replace(panel, [
      controls,
      stateBlock({ title: t("stats.growth.empty"), body: t("stats.growth.emptyBody") }),
    ]);
    return null;
  }

  const note = el("p", { class: "panel__note stats-page__note", text: t("stats.growth.note") });
  if (state.view === "graph") {
    const { node, teardown } = growthGraph(counted, { t, language, state });
    replace(panel, [controls, note, node]);
    return teardown;
  }

  replace(panel, [
    controls,
    note,
    growthSummaryTable(summarizeGrowth(counted), { t, language }),
    recentRunsTable(counted, { t, language }),
  ]);
  return null;
}

/* One row of controls above everything they scope. */
function growthControls({ t, state, go }) {
  const group = (label, values, current, name, render) =>
    el(
      "div",
      { class: "chips stats-page__group", role: "group", "aria-label": label },
      values.map((value) =>
        el("button", {
          class: "chip",
          type: "button",
          "aria-pressed": String(value === current),
          text: render(value),
          onClick: () => go({ [name]: value }),
        }),
      ),
    );

  const loads = el("input", {
    type: "checkbox",
    checked: state.loads,
    onChange: (event) => go({ loads: event.currentTarget.checked }),
  });

  return el("div", { class: "stats-page__controls" }, [
    group(`${t("stats.view.table")} / ${t("stats.view.graph")}`, VIEWS, state.view, "view", (value) => t(`stats.view.${value}`)),
    group(t("stats.col.when"), RANGES, state.range, "range", (value) => t(`stats.range.${value}`)),
    state.view === "graph"
      ? group(t("stats.metric.offers"), METRICS, state.metric, "metric", (value) =>
          t(`stats.metric.${value}`),
        )
      : null,
    el("label", { class: "stats-page__check" }, [loads, el("span", { text: t("stats.loads") })]),
  ]);
}

function table(columns, rows, { footer = null } = {}) {
  return el(
    "div",
    { class: "table-scroll" },
    el("table", { class: "sources stats-table" }, [
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
      el("tbody", {}, rows),
      footer ? el("tfoot", {}, footer) : null,
    ]),
  );
}

function serviceCell(key, name, extras = []) {
  return el("td", {}, [
    el("span", {
      class: "stats-page__dot",
      "aria-hidden": "true",
      style: { "--source-color": sourceColorVar(key) },
    }),
    el("span", { class: "source__name", text: name }),
    ...extras,
  ]);
}

const num = (value, language) => el("td", { class: "num stats-table__n", text: formatCount(value, language) });

function growthSummaryTable(rows, { t, language }) {
  const totals = rows.reduce(
    (sum, row) => ({
      syncs: sum.syncs + row.syncs,
      offers_added: sum.offers_added + row.offers_added,
      titles_created: sum.titles_created + row.titles_created,
      offers_retired: sum.offers_retired + row.offers_retired,
    }),
    { syncs: 0, offers_added: 0, titles_created: 0, offers_retired: 0 },
  );

  return el("section", { class: "panel" }, [
    el("h2", { class: "panel__title", text: t("stats.growth.byService") }),
    table(
      [
        t("stats.col.service"),
        t("stats.col.syncs"),
        t("stats.col.offersAdded"),
        t("stats.col.titlesCreated"),
        t("stats.col.retired"),
        t("stats.col.lastSync"),
      ],
      rows.map((row) =>
        el("tr", {}, [
          serviceCell(row.source_key, row.source_name),
          num(row.syncs, language),
          num(row.offers_added, language),
          num(row.titles_created, language),
          num(row.offers_retired, language),
          el("td", { class: "num source__synced", text: formatDate(row.last_sync, language) }),
        ]),
      ),
      {
        footer: el("tr", {}, [
          el("th", { scope: "row", text: t("stats.total") }),
          num(totals.syncs, language),
          num(totals.offers_added, language),
          num(totals.titles_created, language),
          num(totals.offers_retired, language),
          el("td", {}),
        ]),
      },
    ),
  ]);
}

function recentRunsTable(runs, { t, language }) {
  const latest = runs.slice(-RECENT_RUNS).reverse();
  return el("section", { class: "panel" }, [
    el("h2", { class: "panel__title", text: t("stats.growth.recent") }),
    runs.length > RECENT_RUNS
      ? el("p", {
          class: "panel__note",
          text: t("stats.growth.recentNote", { shown: RECENT_RUNS, total: runs.length }),
        })
      : null,
    table(
      [
        t("stats.col.service"),
        t("stats.col.when"),
        t("stats.col.offersAdded"),
        t("stats.col.titlesCreated"),
        t("stats.col.retired"),
        t("stats.col.status"),
      ],
      latest.map((run) =>
        el("tr", {}, [
          serviceCell(
            run.source_key,
            run.source_name,
            run.catalog_load ? [el("span", { class: "badge", text: t("stats.loadBadge") })] : [],
          ),
          el("td", { class: "num source__synced", text: formatDate(run.started_at, language) }),
          num(run.offers_added, language),
          num(run.titles_created, language),
          num(run.offers_retired, language),
          el(
            "td",
            { class: "num" },
            el("span", { class: `badge badge--${run.status}`, text: run.status.replace(/_/g, " ") }),
          ),
        ]),
      ),
    ),
  ]);
}

/* -- the graph -------------------------------------------------------------- */

const SVG_NS = "http://www.w3.org/2000/svg";
const CHART = { width: 320, height: 96, top: 6, bottom: 4 };
const BAR_GAP = 2;
const BAR_MAX = 24;

function svg(tag, attributes = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
  return node;
}

/** A column with a rounded data end and a square foot on the baseline. */
export function columnPath(x, y, width, height) {
  if (height <= 0) return "";
  const r = Math.min(4, width / 2, height);
  const base = y + height;
  return [
    `M${x},${base}`,
    `V${y + r}`,
    `Q${x},${y} ${x + r},${y}`,
    `H${x + width - r}`,
    `Q${x + width},${y} ${x + width},${y + r}`,
    `V${base}`,
    "Z",
  ].join(" ");
}

function growthGraph(runs, { t, language, state }) {
  const { unit, starts } = bucketsFor(runs, { days: state.range });
  const series = growthSeries(runs, { metric: state.metric, unit, starts });
  const shown = series.filter((entry) => entry.total > 0);
  const quiet = series.length - shown.length;

  const tip = el("div", { class: "chart-tip", role: "status", hidden: true });
  const dateFormat = new Intl.DateTimeFormat(language === "he" ? "he-IL" : "en-GB", {
    day: unit === "month" ? undefined : "numeric",
    month: "short",
    year: unit === "day" ? undefined : "numeric",
  });

  const grid = el(
    "ul",
    { class: "multiples" },
    shown.map((entry) => multiple(entry, { t, language, unit, starts, tip, dateFormat })),
  );

  const node = el("div", { class: "multiples__wrap" }, [
    el("p", {
      class: "panel__note",
      text: `${t(`stats.metric.${state.metric}`)} · ${t(`stats.graph.bucket.${unit}`)}. ${t("stats.graph.note")}`,
    }),
    grid,
    quiet ? el("p", { class: "muted", text: t("stats.graph.quiet", { count: quiet }) }) : null,
    tip,
  ]);

  return { node, teardown: () => tip.remove() };
}

function multiple(entry, { t, language, unit, starts, tip, dateFormat }) {
  const peak = Math.max(...entry.values);
  const slot = CHART.width / starts.length;
  const barWidth = Math.max(1, Math.min(BAR_MAX, slot - BAR_GAP));
  const plot = CHART.height - CHART.top - CHART.bottom;

  const chart = svg("svg", {
    class: "multiple__chart",
    viewBox: `0 0 ${CHART.width} ${CHART.height}`,
    preserveAspectRatio: "none",
    role: "img",
    tabindex: "0",
    "aria-label": `${entry.source_name}: ${formatCount(entry.total, language)}`,
  });
  chart.style.setProperty("--source-color", sourceColorVar(entry.source_key));

  chart.append(
    svg("line", {
      class: "multiple__baseline",
      x1: 0,
      x2: CHART.width,
      y1: CHART.height - CHART.bottom,
      y2: CHART.height - CHART.bottom,
    }),
  );

  const bars = entry.values.map((value, position) => {
    const height = peak ? (value / peak) * plot : 0;
    const x = position * slot + (slot - barWidth) / 2;
    const y = CHART.top + plot - height;
    const bar = svg("path", { class: "multiple__bar", d: columnPath(x, y, barWidth, height) });
    chart.append(bar);
    return bar;
  });

  let active = -1;
  const show = (position, anchor) => {
    if (active >= 0) bars[active].classList.remove("multiple__bar--on");
    active = position;
    bars[position].classList.add("multiple__bar--on");

    const value = entry.values[position];
    replace(tip, [
      el("strong", { class: "chart-tip__value", text: formatCount(value, language) }),
      el("span", { class: "chart-tip__label", text: entry.source_name }),
      el("span", { class: "chart-tip__label", text: dateFormat.format(new Date(starts[position])) }),
    ]);
    tip.hidden = false;

    const box = chart.getBoundingClientRect();
    const host = tip.parentElement.getBoundingClientRect();
    const x = box.left - host.left + ((position + 0.5) / starts.length) * box.width;
    tip.style.setProperty("--x", `${x}px`);
    tip.style.setProperty("--y", `${(anchor ?? box.top) - host.top}px`);
  };
  const hide = () => {
    if (active >= 0) bars[active].classList.remove("multiple__bar--on");
    active = -1;
    tip.hidden = true;
  };
  const positionAt = (clientX) => {
    const box = chart.getBoundingClientRect();
    const fraction = (clientX - box.left) / box.width;
    return Math.min(starts.length - 1, Math.max(0, Math.floor(fraction * starts.length)));
  };

  // The whole chart is the hit target: a reader aims at a week, not a 3px bar.
  chart.addEventListener("pointermove", (event) => show(positionAt(event.clientX)));
  chart.addEventListener("pointerleave", hide);
  chart.addEventListener("blur", hide);
  chart.addEventListener("keydown", (event) => {
    const step = { ArrowRight: 1, ArrowLeft: -1, Home: -Infinity, End: Infinity }[event.key];
    if (step === undefined) return;
    event.preventDefault();
    const from = active < 0 ? starts.length - 1 : active;
    show(Math.min(starts.length - 1, Math.max(0, from + step)));
  });

  return el("li", { class: "multiple" }, [
    el("div", { class: "multiple__head" }, [
      el("span", {
        class: "stats-page__dot",
        "aria-hidden": "true",
        style: { "--source-color": sourceColorVar(entry.source_key) },
      }),
      el("span", { class: "multiple__name", text: entry.source_name }),
      el("span", { class: "multiple__total", text: formatCount(entry.total, language) }),
    ]),
    el("div", { class: "multiple__plot", dir: "ltr" }, chart),
    el("div", { class: "multiple__axis", dir: "ltr" }, [
      el("span", { text: dateFormat.format(new Date(starts[0])) }),
      el("span", { text: t("stats.graph.peak", { value: formatCount(peak, language) }) }),
      el("span", { text: dateFormat.format(new Date(starts[starts.length - 1])) }),
    ]),
  ]);
}

/* -- services --------------------------------------------------------------- */

async function servicesPanel(panel, { app }) {
  const { t, language } = app.get();
  loading(panel, t);

  let snapshot;
  try {
    snapshot = await getServiceStats();
  } catch (error) {
    failed(panel, error, t, () => servicesPanel(panel, { app }));
    return null;
  }

  const services = [...snapshot.services].sort(
    (a, b) => b.titles - a.titles || a.name.localeCompare(b.name),
  );

  replace(panel, [
    el(
      "ul",
      { class: "stats" },
      [
        ["stats.col.titles", snapshot.titles],
        ["stats.col.movies", snapshot.movies],
        ["stats.col.series", snapshot.series],
        ["stats.col.offers", snapshot.offers],
      ].map(([label, value]) =>
        el("li", { class: "stat" }, [
          el("span", { class: "stat__value", text: formatCount(value, language) }),
          el("span", { class: "stat__label", text: t(label) }),
        ]),
      ),
    ),
    servicesTable(services, { t, language, snapshot }),
    pricesTable(services, { t, language }),
  ]);
  return null;
}

function servicesTable(services, { t, language, snapshot }) {
  const byType = (service, type) => service.offers_by_type?.[type] ?? 0;
  const sum = (type) => services.reduce((total, service) => total + byType(service, type), 0);

  return el("section", { class: "panel" }, [
    el("h2", { class: "panel__title", text: t("stats.tab.services") }),
    el("p", { class: "panel__note", text: t("stats.services.note") }),
    table(
      [
        t("stats.col.service"),
        t("stats.col.titles"),
        t("stats.col.movies"),
        t("stats.col.series"),
        t("stats.col.offers"),
        t("stats.col.stream"),
        t("stats.col.free"),
        t("stats.col.rent"),
        t("stats.col.buy"),
        t("stats.col.lastSync"),
      ],
      services.map((service) =>
        el("tr", { class: service.active ? "" : "stats-table__retired" }, [
          serviceCell(
            service.key,
            service.name,
            service.active ? [] : [el("span", { class: "badge", text: t("manage.source.retired") })],
          ),
          num(service.titles, language),
          num(service.movies, language),
          num(service.series, language),
          num(service.offers, language),
          num(byType(service, "stream"), language),
          num(byType(service, "free"), language),
          num(byType(service, "rent"), language),
          num(byType(service, "buy"), language),
          el("td", {
            class: "num source__synced",
            text: service.last_synced_at
              ? formatDate(service.last_synced_at, language)
              : t("manage.stat.never"),
          }),
        ]),
      ),
      {
        footer: el("tr", {}, [
          el("th", { scope: "row", text: t("stats.total") }),
          num(snapshot.titles, language),
          num(snapshot.movies, language),
          num(snapshot.series, language),
          num(snapshot.offers, language),
          num(sum("stream"), language),
          num(sum("free"), language),
          num(sum("rent"), language),
          num(sum("buy"), language),
          el("td", {}),
        ]),
      },
    ),
  ]);
}

/** One row per service and paid deal that has any offers - the price_coverage.py table. */
export function priceRows(services) {
  const rows = [];
  for (const service of services) {
    for (const deal of ["rent", "buy"]) {
      const coverage = service[deal];
      if (coverage?.offers) rows.push({ service, deal, ...coverage });
    }
  }
  return rows;
}

function pricesTable(services, { t, language }) {
  const rows = priceRows(services);
  const heading = el("h2", { class: "panel__title", text: t("stats.prices") });

  if (!rows.length) {
    return el("section", { class: "panel" }, [
      heading,
      el("p", { class: "muted", text: t("stats.prices.empty") }),
    ]);
  }

  const offers = rows.reduce((total, row) => total + row.offers, 0);
  const priced = rows.reduce((total, row) => total + row.priced, 0);

  const coverageCell = (part, whole) => {
    const percent = share(part, whole);
    return el("td", { class: "num" }, [
      el("span", { class: `pct pct--${percentBand(percent)}`, text: formatPercent(percent) }),
      el("span", {
        class: "mix__bar stats-table__bar",
        "aria-hidden": "true",
        style: { "--fill": `${percent ?? 0}%` },
      }),
    ]);
  };

  return el("section", { class: "panel" }, [
    heading,
    el("p", { class: "panel__note", text: t("stats.prices.note") }),
    table(
      [
        t("stats.col.service"),
        t("stats.col.deal"),
        t("stats.col.offers"),
        t("stats.col.priced"),
        t("stats.col.unpriced"),
        t("stats.col.coverage"),
        t("stats.col.titlesPriced"),
      ],
      rows.map((row) =>
        el("tr", {}, [
          serviceCell(row.service.key, row.service.name),
          el("td", { class: "num", text: t(`stats.col.${row.deal}`) }),
          num(row.offers, language),
          num(row.priced, language),
          num(row.unpriced, language),
          coverageCell(row.priced, row.offers),
          num(row.titles_priced, language),
        ]),
      ),
      {
        footer: el("tr", {}, [
          el("th", { scope: "row", text: t("stats.total") }),
          el("td", {}),
          num(offers, language),
          num(priced, language),
          num(offers - priced, language),
          coverageCell(priced, offers),
          el("td", {}),
        ]),
      },
    ),
  ]);
}
