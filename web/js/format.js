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
  return score === null || score === undefined ? "-" : String(score);
}

/** Vote counts, abbreviated so a card stays readable. */
export function formatVotes(votes, language = "en") {
  if (!votes) return "";
  const locale = language === "he" ? "he-IL" : "en-US";
  return new Intl.NumberFormat(locale, { notation: "compact" }).format(votes);
}

/**
 * What an offer costs, as its own currency writes it.
 *
 * Stored in the currency's minor unit, so 1990 ILS-minor is ₪19.90. Returns ""
 * when a source charges nothing per title, which is most of them: an offer
 * included in a subscription must not read as costing zero.
 */
export function formatPrice(minor, currency, language = "en") {
  if (minor === null || minor === undefined || !currency) return "";
  const locale = language === "he" ? "he-IL" : "en-IL";
  try {
    return new Intl.NumberFormat(locale, { style: "currency", currency }).format(minor / 100);
  } catch {
    // An unknown currency code is worth showing as a number, not swallowing.
    return `${(minor / 100).toFixed(2)} ${currency}`;
  }
}

/* How long a film feels, in four bands, so a viewer deciding what to start
 * tonight can tell at a glance. The boundaries are the ones film itself uses:
 * under an hour and a half is a short evening, two hours is the standard
 * feature, and past two and a half you are committing to something. */
export const RUNTIME_BANDS = [
  { under: 90, level: 1, key: "runtime.short" },
  { under: 120, level: 2, key: "runtime.standard" },
  { under: 150, level: 3, key: "runtime.long" },
  { under: Infinity, level: 4, key: "runtime.epic" },
];

/** Which band a runtime falls in, or null when there is no runtime to band. */
export function runtimeBand(minutes) {
  if (typeof minutes !== "number" || !Number.isFinite(minutes) || minutes <= 0) return null;
  return RUNTIME_BANDS.find((band) => minutes < band.under) ?? null;
}

/**
 * An ISO 3166-1 code as its flag: "IL" becomes 🇮🇱.
 *
 * Built from regional indicator letters rather than an image, so it costs no
 * request and inherits the text colour. Windows has no flag glyphs and will
 * show the two letters instead, which is a legible fallback rather than tofu.
 */
export function countryFlag(code) {
  if (typeof code !== "string" || !/^[A-Za-z]{2}$/.test(code)) return "";
  const FIRST_REGIONAL_INDICATOR = 0x1f1e6; // 🇦
  return [...code.toUpperCase()]
    .map((letter) => String.fromCodePoint(FIRST_REGIONAL_INDICATOR + letter.charCodeAt(0) - 65))
    .join("");
}

/* Region and language names, resolved by the browser rather than by a table we
 * would have to translate and maintain. Cached because constructing one of
 * these is not cheap and a page can ask for a dozen. */
const displayNames = new Map();

function namer(language, type) {
  const locale = language === "he" ? "he-IL" : "en";
  const key = `${locale}:${type}`;
  if (!displayNames.has(key)) {
    try {
      displayNames.set(key, new Intl.DisplayNames([locale], { type }));
    } catch {
      displayNames.set(key, null);
    }
  }
  return displayNames.get(key);
}

/**
 * An ISO 639-1 code as a language name: "he" reads "עברית" or "Hebrew".
 *
 * The code itself is the fallback, which is honest - better a reader sees
 * "yue" than nothing at all.
 */
export function languageName(code, language = "en") {
  if (!code) return "";
  try {
    return namer(language, "language")?.of(code) || code;
  } catch {
    return code;
  }
}

/** An ISO 3166-1 code as a country name: "IL" reads "ישראל" or "Israel". */
export function countryName(code, language = "en") {
  if (!code) return "";
  try {
    return namer(language, "region")?.of(code) || code;
  } catch {
    return code;
  }
}

/** A person's name in the reader's language, falling back to the other. */
export function personName(person, language = "en") {
  if (!person) return "";
  return language === "he"
    ? person.name_he || person.name_en || ""
    : person.name_en || person.name_he || "";
}

/** A date as the viewer's locale writes it. */
export function formatDate(value, language = "en") {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const locale = language === "he" ? "he-IL" : "en-GB";
  return new Intl.DateTimeFormat(locale, { dateStyle: "medium" }).format(date);
}

/* Past a month "37 days ago" stops meaning anything and a date starts meaning
 * something again. */
const RELATIVE_DAYS = 30;

/**
 * A day as "today", "yesterday" or "5 days ago" - and as a date once that
 * stops being useful.
 *
 * Both halves come from Intl rather than from strings we would have to
 * translate: the reader's own language already has words for this, and one of
 * ours would be a worse copy of them. A date in the future is somebody's clock
 * disagreeing with ours, not news from tomorrow, so it is shown as a date.
 */
export function formatWhen(value, language = "en", now = new Date()) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";

  const days = Math.round((startOfDay(date) - startOfDay(now)) / 86_400_000);
  if (days > 0 || days < -RELATIVE_DAYS) return formatDate(value, language);

  const locale = language === "he" ? "he-IL" : "en-GB";
  try {
    // "auto" is what turns -1 into "yesterday" rather than "1 day ago".
    return new Intl.RelativeTimeFormat(locale, { numeric: "auto" }).format(days, "day");
  } catch {
    return formatDate(value, language);
  }
}

/** Midnight, so "yesterday" is a day apart rather than 24 hours apart. */
function startOfDay(date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
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

/* How many votes a score has to rest on before it is shown plainly.
 *
 * Mirrors the confidence point the aggregate itself uses, and is here for the
 * same reason the score bands are: what a number looks like is a decision
 * about reading it, not about computing it. Under this the score has been
 * pulled towards the ordinary and is more a statement about the catalog than
 * about the title, so it is shown faintly - the figure is still there for
 * anybody who wants it, it just stops competing for the eye with scores that
 * thousands of people stand behind.
 */
export const CONFIDENT_VOTES = 100;

/** Whether a score rests on too little to be read at face value.
 *
 * A title with no score is not a thinly-held one: it has nothing to hold. Its
 * pill shows a dash, and dressing that up as low confidence would explain a
 * number that is not there - and explain it wrongly, since the reason is more
 * often a single rater than a shortage of voters.
 */
export function scoreIsThin(score, votes) {
  if (score === null || score === undefined) return false;
  return votes !== null && votes !== undefined && votes < CONFIDENT_VOTES;
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

/* The order offers read in when a service has more than one: what you can
 * watch for nothing first, then what a subscription covers, then what costs
 * money - cheapest commitment first. Not the enum's order, which is the order
 * they were written down in. */
const OFFER_ORDER = ["free", "stream", "rent", "buy"];

/**
 * One row per service, not one per deal.
 *
 * Apple TV Store sells and rents almost everything, so a film listed there
 * produced two rows with the same name, the same date and, since neither
 * carries a price, nothing whatever to tell them apart - 15,576 titles of
 * them. What a reader wants to know is that the film is on Apple TV and what
 * it would cost; that is one row saying "Rent · Buy".
 *
 * Grouped by state as well as by service, so a service that still rents a
 * title it no longer sells stays two rows. Merging those would put "Rent ·
 * Buy" next to one badge and claim something the catalog does not know.
 */
export function offersByService(availability) {
  const groups = new Map();
  for (const offer of availability) {
    const state = offerState(offer);
    const key = `${offer.source_key}\u0000${state}`;
    const group = groups.get(key);
    if (group) {
      group.offers.push(offer);
      continue;
    }
    groups.set(key, { ...offer, state, offers: [offer] });
  }

  return [...groups.values()]
    .map((group) => ({
      ...group,
      offers: [...group.offers].sort(
        (a, b) => OFFER_ORDER.indexOf(a.offer_type) - OFFER_ORDER.indexOf(b.offer_type),
      ),
      // The freshest sighting of any of them: the row says when the service was
      // last seen carrying this title, and one deal is enough for that.
      last_seen: group.offers.reduce(
        (latest, offer) => (offer.last_seen > latest ? offer.last_seen : latest),
        group.offers[0].last_seen,
      ),
      // Whichever deal knows where it goes. They are the same page.
      deep_link_url: group.offers.find((offer) => offer.deep_link_url)?.deep_link_url ?? null,
    }))
    .sort((a, b) => Number(b.is_current) - Number(a.is_current));
}

/**
 * What the catalog shows when nobody has asked for anything.
 *
 * One place rather than a default repeated at every reader: the empty state's
 * "clear filters" used to reset the four it knew about and silently keep the
 * rest, which left you looking at no results with no visible reason why.
 */
export const DEFAULT_FILTERS = Object.freeze({
  q: "",
  sources: [],
  type: "",
  available: "current",
  sort: "",
  order: "",
  yearMin: "",
  yearMax: "",
  genres: [],
  scoreMin: "",
  runtimeMax: "",
});

/** Query-string parameters from the current filter state, omitting defaults. */
export function filtersToParams(filters) {
  const params = new URLSearchParams();
  if (filters.q) params.set("q", filters.q);
  if (filters.sources?.length) params.set("sources", filters.sources.join(","));
  if (filters.type) params.set("type", filters.type);
  if (filters.available && filters.available !== "current") {
    params.set("available", filters.available);
  }
  if (filters.sort) params.set("sort", filters.sort);
  if (filters.order) params.set("order", filters.order);
  if (filters.yearMin) params.set("year_min", String(filters.yearMin));
  if (filters.yearMax) params.set("year_max", String(filters.yearMax));
  if (filters.genres?.length) params.set("genres", filters.genres.join(","));
  if (filters.scoreMin) params.set("score_min", String(filters.scoreMin));
  if (filters.runtimeMax) params.set("runtime_max", String(filters.runtimeMax));
  return params;
}

/** Filter state parsed back out of a query string. */
export function paramsToFilters(search) {
  const params = new URLSearchParams(search);
  const list = (name) => (params.get(name) ?? "").split(",").filter(Boolean);
  return {
    ...DEFAULT_FILTERS,
    q: params.get("q") ?? "",
    sources: list("sources"),
    type: params.get("type") ?? "",
    available: params.get("available") ?? "current",
    sort: params.get("sort") ?? "",
    order: params.get("order") ?? "",
    yearMin: params.get("year_min") ?? "",
    yearMax: params.get("year_max") ?? "",
    genres: list("genres"),
    scoreMin: params.get("score_min") ?? "",
    runtimeMax: params.get("runtime_max") ?? "",
  };
}
