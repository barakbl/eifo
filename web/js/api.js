/* The API client.
 *
 * Every failure arrives as an ApiError carrying the problem-details the server
 * sent, so views have one error shape to render rather than a mix of thrown
 * strings, rejected promises and undefined.
 */

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
    return me.user;
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

/** Build a my-list query for one of the tabs. */
export function myItemsQuery({ status = "", rated = false } = {}, { page = 1, pageSize = 24 } = {}) {
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  if (rated) params.set("rated", "true");
  params.set("page", String(page));
  params.set("page_size", String(pageSize));
  return params.toString();
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
