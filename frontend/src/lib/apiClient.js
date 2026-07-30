// Thin fetch wrapper for the six task endpoints + /lanes. Bare paths, no
// /api prefix -- that's what the backend actually mounts (see
// vite.config.js) and it's the exact contract tests/ exercise directly.

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `Request failed (${status})`);
    this.status = status;
    this.detail = detail;
  }
}

let unauthorizedHandler = null;

// AuthContext registers this once, so ANY call anywhere that hits a 401
// (key revoked, typo'd manually into localStorage, etc) routes back
// through one place instead of every call site re-deciding what a 401
// means.
export function setUnauthorizedHandler(fn) {
  unauthorizedHandler = fn;
}

async function request(path, { method = "GET", apiKey, params, body, signal } = {}) {
  const url = new URL(path, window.location.origin);
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value === undefined || value === null || value === "") continue;
      if (Array.isArray(value)) {
        // Repeated params (?status=queued&status=running), not a single
        // comma-joined value -- matches api/routes.py's Query(None)
        // collection, and is the standard REST convention for a
        // multi-value filter.
        for (const v of value) url.searchParams.append(key, v);
      } else {
        url.searchParams.set(key, value);
      }
    }
  }

  const headers = {};
  if (apiKey) headers["X-API-Key"] = apiKey;
  if (body !== undefined) headers["Content-Type"] = "application/json";

  let res;
  try {
    res = await fetch(url.toString(), {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal,
    });
  } catch (err) {
    throw new ApiError(0, "Could not reach the server. Check your connection.");
  }

  if (res.status === 401) {
    unauthorizedHandler?.();
    throw new ApiError(401, "Missing or invalid API key");
  }

  if (!res.ok) {
    let detail;
    try {
      detail = (await res.json()).detail;
    } catch {
      detail = undefined;
    }
    throw new ApiError(res.status, detail);
  }

  if (res.status === 204) return null;
  return res.json();
}

export function listLanes(apiKey, signal) {
  return request("/lanes", { apiKey, signal });
}

export function listTasks(apiKey, filters = {}, signal) {
  const { status, lane, createdAfter, createdBefore, sortBy, sortOrder, limit, offset } = filters;
  return request("/tasks", {
    apiKey,
    signal,
    params: {
      status,
      lane,
      created_after: createdAfter,
      created_before: createdBefore,
      sort_by: sortBy,
      sort_order: sortOrder,
      limit,
      offset,
    },
  });
}

export function getTaskDetail(apiKey, handle, signal) {
  return request(`/tasks/${encodeURIComponent(handle)}`, { apiKey, signal });
}

export function submitTask(apiKey, lane, params) {
  return request("/tasks", { apiKey, method: "POST", body: { lane, params } });
}

export function collectResult(apiKey, handle) {
  return request(`/tasks/${encodeURIComponent(handle)}/result`, { apiKey });
}

export function cancelTask(apiKey, handle) {
  return request(`/tasks/${encodeURIComponent(handle)}/cancel`, { apiKey, method: "POST" });
}

export function retryTask(apiKey, handle) {
  return request(`/tasks/${encodeURIComponent(handle)}/retry`, { apiKey, method: "POST" });
}
