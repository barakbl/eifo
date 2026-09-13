import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  bucketStart,
  bucketUnit,
  bucketsFor,
  columnPath,
  countedRuns,
  growthSeries,
  priceRows,
  readStatsState,
  summarizeGrowth,
  writeStatsState,
} from "../js/views/stats.js";

const run = (key, startedAt, offers, titles, extra = {}) => ({
  source_key: key,
  source_name: key.toUpperCase(),
  started_at: startedAt,
  offers_added: offers,
  titles_created: titles,
  offers_retired: 0,
  catalog_load: false,
  ...extra,
});

describe("the stats URL", () => {
  it("defaults everything a link leaves out", () => {
    assert.deepEqual(readStatsState(""), {
      tab: "growth",
      view: "table",
      metric: "offers",
      range: 90,
      loads: false,
    });
  });

  it("refuses values it does not know rather than rendering them", () => {
    const state = readStatsState("tab=secret&view=pie&range=7&metric=votes");

    assert.equal(state.tab, "growth");
    assert.equal(state.view, "table");
    assert.equal(state.range, 90);
    assert.equal(state.metric, "offers");
  });

  it("writes only what differs from the defaults, and reads it back", () => {
    assert.equal(writeStatsState(readStatsState("")), "");

    const chosen = { tab: "growth", view: "graph", metric: "titles", range: 365, loads: true };
    assert.deepEqual(readStatsState(writeStatsState(chosen)), chosen);
  });
});

describe("buckets", () => {
  it("widens with the range so a chart keeps a readable number of bars", () => {
    assert.equal(bucketUnit(30), "day");
    assert.equal(bucketUnit(90), "week");
    assert.equal(bucketUnit(365), "week");
    assert.equal(bucketUnit(3650), "month");
  });

  it("starts weeks on Sunday, the first day of the Israeli week", () => {
    // Wednesday 9 September 2026.
    const start = new Date(bucketStart("2026-09-09T20:00:00Z", "week"));

    assert.equal(start.toISOString(), "2026-09-06T00:00:00.000Z");
    assert.equal(start.getUTCDay(), 0);
  });

  it("puts every moment of a month in that month's bucket", () => {
    assert.equal(bucketStart("2026-02-28T23:59:00Z", "month"), Date.UTC(2026, 1, 1));
  });

  it("covers the range up to and including today", () => {
    const now = Date.UTC(2026, 8, 13, 12);
    const { unit, starts } = bucketsFor([], { days: 30, now });

    assert.equal(unit, "day");
    assert.equal(starts.at(-1), Date.UTC(2026, 8, 13));
    assert.equal(starts.length, 31);
  });

  it("starts 'all' at the first sync, sized to the history there is", () => {
    const now = Date.UTC(2026, 8, 13);
    const { unit, starts } = bucketsFor([run("mako", "2026-08-02T01:00:00Z", 1, 1)], {
      days: 3650,
      now,
    });

    // Six weeks of history is weekly bars, not two months.
    assert.equal(unit, "week");
    assert.equal(starts[0], Date.UTC(2026, 7, 2));
    assert.equal(starts.length, 7);
  });

  it("goes monthly once the history is longer than a year", () => {
    const now = Date.UTC(2026, 8, 13);
    const { unit, starts } = bucketsFor([run("mako", "2025-01-20T01:00:00Z", 1, 1)], {
      days: 3650,
      now,
    });

    assert.equal(unit, "month");
    assert.equal(starts[0], Date.UTC(2025, 0, 1));
  });
});

describe("growth", () => {
  const runs = [
    run("mako", "2026-09-01T01:00:00Z", 900, 800, { catalog_load: true }),
    run("mako", "2026-09-02T01:00:00Z", 5, 2),
    run("netflix_il", "2026-09-02T01:00:00Z", 30, 1),
    run("mako", "2026-09-10T01:00:00Z", 4, 0),
  ];

  it("hides the syncs that loaded a catalog unless asked", () => {
    assert.equal(countedRuns(runs, { loads: false }).length, 3);
    assert.equal(countedRuns(runs, { loads: true }).length, 4);
  });

  it("totals per service, most added first", () => {
    const rows = summarizeGrowth(countedRuns(runs, { loads: false }));

    assert.deepEqual(
      rows.map((row) => [row.source_key, row.syncs, row.offers_added, row.titles_created]),
      [
        ["netflix_il", 1, 30, 1],
        ["mako", 2, 9, 2],
      ],
    );
    assert.equal(rows[1].last_sync, "2026-09-10T01:00:00Z");
  });

  it("lays each service into the same buckets, zeroes included", () => {
    const now = Date.UTC(2026, 8, 13);
    const { unit, starts } = bucketsFor(runs, { days: 30, now });
    const series = growthSeries(countedRuns(runs, { loads: false }), {
      metric: "offers",
      unit,
      starts,
    });

    assert.deepEqual(
      series.map((entry) => entry.source_key),
      ["netflix_il", "mako"],
    );
    for (const entry of series) assert.equal(entry.values.length, starts.length);

    const mako = series[1];
    assert.equal(mako.total, 9);
    assert.equal(mako.values[starts.indexOf(Date.UTC(2026, 8, 2))], 5);
    assert.equal(mako.values[starts.indexOf(Date.UTC(2026, 8, 10))], 4);
  });

  it("charts titles new to the catalog when that is the metric", () => {
    const now = Date.UTC(2026, 8, 13);
    const { unit, starts } = bucketsFor(runs, { days: 30, now });
    const [top] = growthSeries(runs, { metric: "titles", unit, starts });

    assert.equal(top.source_key, "mako");
    assert.equal(top.total, 802);
  });
});

describe("columnPath", () => {
  it("draws nothing for an empty bucket", () => {
    assert.equal(columnPath(0, 10, 6, 0), "");
  });

  it("never rounds a corner wider than half the bar", () => {
    // A 3px bar: a 4px radius would cross itself.
    assert.match(columnPath(0, 0, 3, 50), /Q0,0 1\.5,0/);
  });
});

describe("priceRows", () => {
  it("lists only the deals a service actually has, rent before buy", () => {
    const services = [
      { key: "netflix_il", rent: { offers: 0 }, buy: { offers: 0 } },
      {
        key: "apple_tv_store",
        rent: { offers: 10, priced: 4, unpriced: 6, titles_priced: 4 },
        buy: { offers: 12, priced: 0, unpriced: 12, titles_priced: 0 },
      },
    ];

    assert.deepEqual(
      priceRows(services).map((row) => [row.service.key, row.deal, row.priced]),
      [
        ["apple_tv_store", "rent", 4],
        ["apple_tv_store", "buy", 0],
      ],
    );
  });
});
