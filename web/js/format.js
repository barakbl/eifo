/* Pure formatting helpers.
 *
 * Kept free of DOM and network access so they can be tested with `node --test`
 * and reused wherever a value needs presenting.
 */

/** Score bands, matching the colour classes in app.css. */
export const SCORE_HIGH = 75;
export const SCORE_MID = 50;

/**
 * Which band a 0-100 score falls in.
 *
 * A missing score is its own band rather than a zero: "not rated yet" and
 * "rated badly" must not look the same.
 */
export function scoreBand(score) {
  if (score === null || score === undefined) return "none";
  if (score >= SCORE_HIGH) return "high";
  if (score >= SCORE_MID) return "mid";
  return "low";
}

/** A score for display, or an em dash when there is none. */
export function formatScore(score) {
  return score === null || score === undefined ? "—" : String(score);
}

/** Vote counts, abbreviated so a card stays readable. */
export function formatVotes(votes, language = "en") {
  if (!votes) return "";
  const locale = language === "he" ? "he-IL" : "en-US";
  return new Intl.NumberFormat(locale, { notation: "compact" }).format(votes);
}

/** A date as the viewer's locale writes it. */
export function formatDate(value, language = "en") {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const locale = language === "he" ? "he-IL" : "en-GB";
  return new Intl.DateTimeFormat(locale, { dateStyle: "medium" }).format(date);
}

/**
 * The CSS custom property carrying a source's colour.
 *
 * Unknown keys fall back to a neutral rather than being assigned a colour at
 * random: an unfamiliar service should look unfamiliar, not like another one.
 */
export function sourceColorVar(sourceKey) {
  return /^[a-z0-9_]+$/.test(sourceKey ?? "")
    ? `var(--source-${sourceKey}, var(--source-unknown))`
    : "var(--source-unknown)";
}

/**
 * The distinct services a title is currently on, in a stable order.
 *
 * Feeds the spine on a card, so the same title always shows the same sequence
 * of colours rather than reshuffling between renders.
 */
export function currentSources(availability = []) {
  const keys = [];
  for (const offer of availability) {
    if (offer.is_current && offer.source_active && !keys.includes(offer.source_key)) {
      keys.push(offer.source_key);
    }
  }
  return keys.sort();
}

/**
 * Which badge an offer needs, if any.
 *
 * The two cases are genuinely different: the title left the service, or we
 * stopped tracking the service altogether and cannot vouch for it either way.
 */
export function offerState(offer) {
  if (!offer.source_active) return "untracked";
  if (!offer.is_current) return "gone";
  return "available";
}

/** Query-string parameters from the current filter state, omitting defaults. */
export function filtersToParams(filters) {
  const params = new URLSearchParams();
  if (filters.q) params.set("q", filters.q);
  if (filters.sources?.length) params.set("sources", filters.sources.join(","));
  if (filters.type) params.set("type", filters.type);
  if (filters.available && filters.available !== "current") {
    params.set("available", filters.available);
  }
  if (filters.sort && filters.sort !== "score") params.set("sort", filters.sort);
  return params;
}

/** Filter state parsed back out of a query string. */
export function paramsToFilters(search) {
  const params = new URLSearchParams(search);
  const sources = params.get("sources");
  return {
    q: params.get("q") ?? "",
    sources: sources ? sources.split(",").filter(Boolean) : [],
    type: params.get("type") ?? "",
    available: params.get("available") ?? "current",
    sort: params.get("sort") ?? "score",
  };
}
