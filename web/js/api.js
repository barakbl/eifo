/* The API client.
 *
 * Every failure arrives as an ApiError carrying the problem-details the server
 * sent, so views have one error shape to render rather than a mix of thrown
 * strings, rejected promises and undefined.
 */

const BASE = "/api/v1";

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

async function request(path, { signal } = {}) {
  let response;
  try {
    response = await fetch(`${BASE}${path}`, {
      signal,
      headers: { Accept: "application/json" },
    });
  } catch (cause) {
    if (cause?.name === "AbortError") throw cause;
    throw new ApiError("Network request failed", { offline: true });
  }

  if (!response.ok) throw await errorFromResponse(response);
  return response.json();
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

export function listSources(options) {
  return request("/sources", options);
}

export function getMeta(options) {
  return request("/meta", options);
}
