/* The API client.
 *
 * Every failure arrives as an ApiError carrying the problem-details the server
 * sent, so views have one error shape to render rather than a mix of thrown
 * strings, rejected promises and undefined.
 */

import { filtersToParams } from "./format.js";

const BASE = "/api/v1";
const CSRF_HEADER = "X-CSRF-Token";

/* The CSRF token for this session, handed out by GET /me.
 *
 * Kept in a module variable rather than storage: it belongs to the session
 * cookie, and anything that outlives the tab is one more thing to invalidate. */
let csrfToken = "";

/** Remember the token a `/me` response supplied. */
export function setCsrfToken(token) {
  csrfToken = token ?? "";
}

export class ApiError extends Error {
  constructor(message, { status = 0, detail = "", offline = false } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.offline = offline;
  }

  /** Whether retrying might plausibly help. */
  get retryable() {
    return this.offline || this.status === 0 || this.status >= 500;
  }
}

/**
 * Turn a failed response into an ApiError.
 *
 * The server speaks RFC 9457, so `title` and `detail` are already meant for
 * this; anything else falls back to the status line.
 */
export async function errorFromResponse(response) {
  let title = `Request failed (${response.status})`;
  let detail = "";

  try {
    const body = await response.json();
    if (body && typeof body === "object") {
      title = body.title || title;
      detail = body.detail || "";
    }
  } catch {
    // A non-JSON error body is not itself an error worth surfacing.
  }

  return new ApiError(title, { status: response.status, detail });
}

async function request(path, { signal, method = "GET", body } = {}) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  // Every state-changing request carries the token; a read never needs one.
  if (method !== "GET" && csrfToken) headers[CSRF_HEADER] = csrfToken;

  let response;
  try {
    response = await fetch(`${BASE}${path}`, {
      signal,
      method,
      headers,
      // The session lives in a cookie the page cannot read.
      credentials: "same-origin",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (cause) {
    if (cause?.name === "AbortError") throw cause;
    throw new ApiError("Network request failed", { offline: true });
  }

  if (!response.ok) throw await errorFromResponse(response);
  return response.status === 204 ? null : response.json();
}

/** Build a titles query from filter state plus paging. */
export function titlesQuery(filters = {}, { page = 1, pageSize = 24 } = {}) {
  const params = new URLSearchParams();
  if (filters.q) params.set("q", filters.q);
  if (filters.sources?.length) params.set("sources", filters.sources.join(","));
  if (filters.type) params.set("type", filters.type);
  if (filters.available) params.set("available", filters.available);
  if (filters.sort) params.set("sort", filters.sort);
  if (filters.order) params.set("order", filters.order);
  if (filters.yearMin) params.set("year_min", String(filters.yearMin));
  if (filters.yearMax) params.set("year_max", String(filters.yearMax));
  if (filters.genres?.length) params.set("genres", filters.genres.join(","));
  if (filters.scoreMin) params.set("score_min", String(filters.scoreMin));
  if (filters.runtimeMax) params.set("runtime_max", String(filters.runtimeMax));
  params.set("page", String(page));
  params.set("page_size", String(pageSize));
  return params.toString();
}

export function listTitles(filters, paging, options) {
  return request(`/titles?${titlesQuery(filters, paging)}`, options);
}

export function getTitle(id, options) {
  return request(`/titles/${encodeURIComponent(id)}`, options);
}

export function getPerson(personId, options) {
  return request(`/people/${encodeURIComponent(personId)}`, options);
}

/**
 * Search-as-you-type, narrowed the way the grid behind it is narrowed.
 *
 * A suggestion is a preview of a result, so it takes the same filters. Asked
 * without them the list offered titles the grid then reported did not exist -
 * "batman" with one service selected suggested seven of them above an empty
 * catalog. `q` comes from the box rather than the filters, which hold whatever
 * the last search wrote.
 */
export function suggest(q, filters, options) {
  const params = filtersToParams({ ...(filters ?? {}), q: "" });
  params.delete("q");
  params.set("q", q);
  return request(`/suggest?${params}`, options);
}

/**
 * What turned up on each service lately.
 *
 * `sources` narrows to one service - the page asks for one at a time - and the
 * server answers with arrivals rather than titles: the same film landing on two
 * services is two pieces of news.
 */
export function listWhatsNew({ sources = [] } = {}, { page = 1, pageSize = 24 } = {}, options) {
  const params = new URLSearchParams();
  if (sources.length) params.set("sources", sources.join(","));
  params.set("page", String(page));
  params.set("page_size", String(pageSize));
  return request(`/whats-new?${params}`, options);
}

export function listGenres(options) {
  return request("/genres", options);
}

export function listSources(options) {
  return request("/sources", options);
}

export function getMeta(options) {
  return request("/meta", options);
}

/* -- accounts ------------------------------------------------------------- */

/** Where the login button points. A full navigation, not a fetch: OAuth is a
 * redirect dance the browser has to perform itself. */
export function loginUrl(provider) {
  return `${BASE}/auth/login/${encodeURIComponent(provider)}`;
}

/**
 * The signed-in user, or null.
 *
 * Doubles as the CSRF bootstrap, so it is the first call the app makes and the
 * one every write depends on having succeeded.
 */
export async function getMe(options) {
  try {
    const me = await request("/me", options);
    setCsrfToken(me.csrf_token);
    // Folded onto the user because that is how the client thinks about it -
    // "can I open Manage" is a fact about who is signed in. It only ever hides
    // a link: every endpoint behind the tab checks for itself.
    return { ...me.user, is_admin: Boolean(me.is_admin) };
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      setCsrfToken("");
      return null;
    }
    throw error;
  }
}

export function patchMe(patch) {
  return request("/me", { method: "PATCH", body: patch });
}

export function deleteMe() {
  return request("/me", { method: "DELETE" });
}

export function logout() {
  return request("/auth/logout", { method: "POST" });
}

/** Build a my-list query for one of the tabs, or for a page of the catalog. */
export function myItemsQuery(
  { status = "", rated = false, titleIds = null } = {},
  { page = 1, pageSize = 24 } = {},
) {
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  if (rated) params.set("rated", "true");
  // Repeated rather than comma-joined, which is how FastAPI reads a list.
  for (const id of titleIds ?? []) params.append("title_ids", String(id));
  params.set("page", String(page));
  params.set("page_size", String(pageSize));
  return params.toString();
}

/**
 * How much of one of your lists each service carries, most first.
 *
 * Counted by the server over the whole list rather than by the grid over a
 * page of it: the question is which subscription would clear the watchlist,
 * and twenty-four of them cannot answer that.
 */
export function myListServices(filters, options) {
  const params = new URLSearchParams();
  if (filters?.status) params.set("status", filters.status);
  if (filters?.rated) params.set("rated", "true");
  return request(`/me/items/services?${params}`, options);
}

export function listMyItems(filters, paging, options) {
  return request(`/me/items?${myItemsQuery(filters, paging)}`, options);
}

export function putMyItem(titleId, patch) {
  return request(`/me/items/${encodeURIComponent(titleId)}`, { method: "PUT", body: patch });
}

export function deleteMyItem(titleId) {
  return request(`/me/items/${encodeURIComponent(titleId)}`, { method: "DELETE" });
}

/* -- operator surfaces ----------------------------------------------------
 *
 * Every one of these 404s for a signed-in user who is not an administrator,
 * which is deliberate: the client hides the tab, and the server does not agree
 * that it exists. Both, so neither has to be the only one that is right.
 */

export function listAdminSources(options) {
  return request("/admin/sources", options);
}

export function setSourceEnabled(key, enabled) {
  return request(`/admin/sources/${encodeURIComponent(key)}`, {
    method: "PATCH",
    body: { enabled },
  });
}

/** Whether this instance is private, and which providers can open it.
 *
 * Never gated, and carries nothing about the catalog. It is what a signed-out
 * visitor is answered with on a members-only instance, and the only thing the
 * sign-in wall needs in order to have working buttons on it. */
export function getAuthContext() {
  return request("/auth/context");
}

/* Who may sign in. Administrators only; the endpoints 404 for anybody else. */

export function listMembers() {
  return request("/admin/members");
}

export function inviteMember(email, role = "member") {
  return request("/admin/members", { method: "POST", body: { email, role } });
}

export function setMemberRole(email, role) {
  return request(`/admin/members/${encodeURIComponent(email)}`, {
    method: "PATCH",
    body: { role },
  });
}

export function removeMember(email) {
  return request(`/admin/members/${encodeURIComponent(email)}`, { method: "DELETE" });
}

/* Your own API tokens. Nothing here is an administrator's business. */

export function listMyTokens() {
  return request("/me/tokens");
}

export function createMyToken(name) {
  return request("/me/tokens", { method: "POST", body: { name } });
}

export function revokeMyToken(hint) {
  return request(`/me/tokens/${encodeURIComponent(hint)}`, { method: "DELETE" });
}

/** Build a runs query from the filter chips. */
export function runsQuery(
  { source = "", phase = "", status = "" } = {},
  { page = 1, pageSize = 25 } = {},
) {
  const params = new URLSearchParams();
  if (source) params.set("source", source);
  if (phase) params.set("phase", phase);
  if (status) params.set("status", status);
  params.set("page", String(page));
  params.set("page_size", String(pageSize));
  return params.toString();
}

export function listRuns(filters, paging, options) {
  return request(`/admin/runs?${runsQuery(filters, paging)}`, options);
}

export function getRun(id, options) {
  return request(`/admin/runs/${encodeURIComponent(id)}`, options);
}

export function getAdminStats(options) {
  return request("/admin/stats", options);
}

/* -- the review queue ----------------------------------------------------- */

export function reviewsQuery(
  { source = "", order = "age" } = {},
  { page = 1, pageSize = 25 } = {},
) {
  const params = new URLSearchParams();
  if (source) params.set("source", source);
  if (order) params.set("order", order);
  params.set("page", String(page));
  params.set("page_size", String(pageSize));
  return params.toString();
}

export function listReviews(filters, paging, options) {
  return request(`/reviews?${reviewsQuery(filters, paging)}`, options);
}

export function countReviews(options) {
  return request("/reviews/count", options);
}

export function attachReview(id, titleId) {
  return request(`/reviews/${encodeURIComponent(id)}/attach`, {
    method: "POST",
    body: { title_id: titleId },
  });
}

export function createFromReview(id) {
  return request(`/reviews/${encodeURIComponent(id)}/create`, {
    method: "POST",
  });
}

export function dismissReview(id) {
  return request(`/reviews/${encodeURIComponent(id)}/dismiss`, {
    method: "POST",
  });
}

export function ruleInBulk(ids, decision) {
  return request("/reviews/bulk", { method: "POST", body: { ids, decision } });
}
