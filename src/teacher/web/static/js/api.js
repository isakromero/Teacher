// Thin wrapper over fetch for the JSON API. Every failure becomes an ApiError whose message
// is meant to be shown to the reader: the API's {"detail": ...} text for non-2xx responses,
// a plain explanation for network failures and unexpected (non-JSON) responses.

export class ApiError extends Error {
  constructor(message, { status = 0 } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status; // 0 means the request never got an HTTP response
  }
}

export function detailText(data, fallback) {
  if (data && typeof data.detail === "string" && data.detail) return data.detail;
  // FastAPI validation errors: [{loc, msg, ...}, ...]
  if (data && Array.isArray(data.detail) && data.detail.every((d) => d && typeof d.msg === "string")) {
    return data.detail
      .map((d) => {
        const where = Array.isArray(d.loc) ? d.loc.filter((p) => p !== "body").join(".") : "";
        return where ? `${where}: ${d.msg}` : d.msg;
      })
      .join("; ");
  }
  if (data && data.detail !== undefined) return JSON.stringify(data.detail);
  return fallback;
}

/** `keepalive: true` lets the request finish after the page is closed (the browser allows only
 * small bodies, about 64 KB in all, for such requests). */
export async function request(method, path, { json, form, keepalive = false } = {}) {
  const init = { method, headers: { Accept: "application/json" } };
  if (keepalive) init.keepalive = true;
  if (json !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(json);
  } else if (form !== undefined) {
    init.body = form; // the browser sets the multipart boundary
  }

  let response;
  try {
    response = await fetch(path, init);
  } catch (err) {
    throw new ApiError(
      `Could not reach the server (${err.message}). Check that it is still running, then try again.`
    );
  }

  if (response.status === 204) return null;
  let text;
  try {
    text = await response.text();
  } catch (err) {
    throw new ApiError(`The connection broke while reading the response (${err.message}).`, {
      status: response.status,
    });
  }
  let data = null;
  let parsed = false;
  if (text) {
    try {
      data = JSON.parse(text);
      parsed = true;
    } catch {
      parsed = false;
    }
  }

  if (!response.ok) {
    const fallback = text && !parsed ? text.slice(0, 300) : response.statusText || "request failed";
    throw new ApiError(detailText(data, fallback), { status: response.status });
  }
  if (!parsed) {
    throw new ApiError(`Unexpected response from ${method} ${path}: expected JSON.`, {
      status: response.status,
    });
  }
  return data;
}

export const api = {
  get: (path) => request("GET", path),
  post: (path, json = {}) => request("POST", path, { json }),
  put: (path, json = {}, { keepalive = false } = {}) => request("PUT", path, { json, keepalive }),
  postForm: (path, form) => request("POST", path, { form }),
  delete: (path) => request("DELETE", path),
};

/** A one-line, human-readable description of any error thrown by the API helpers. */
export function describeError(err) {
  if (err instanceof ApiError) {
    return err.status ? `${err.message} (HTTP ${err.status})` : err.message;
  }
  return `Unexpected error: ${err && err.message ? err.message : String(err)}`;
}

/** Encode one path segment (slugs are already URL-safe, but keys come from data). */
export const seg = (value) => encodeURIComponent(String(value));
