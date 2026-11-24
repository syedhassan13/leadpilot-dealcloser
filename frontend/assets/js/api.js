// Small HTTP client: JSON in/out, timeouts, and errors people can read.
import { API } from "./config.js";

export class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

const FRIENDLY = {
  0: "Can't reach LeadPilot right now. Check your connection and try again.",
  400: "Please check the details and try again.",
  401: "Your session has ended. Please sign in again.",
  403: "You don't have access to that.",
  404: "That item no longer exists.",
  409: "That was already handled.",
  422: "Some details look invalid. Please check and try again.",
  429: "Too many requests. Please wait a moment.",
  500: "Something went wrong on our side. Please try again.",
};

export async function request(path, { method = "GET", body, headers = {}, timeout = 45000 } = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  let res;
  try {
    res = await fetch(API + path, {
      method, signal: ctrl.signal,
      headers: { ...(body !== undefined ? { "Content-Type": "application/json" } : {}), ...headers },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    throw new ApiError(0, e.name === "AbortError" ? "This is taking longer than usual. Please try again." : FRIENDLY[0]);
  } finally {
    clearTimeout(timer);
  }
  if (!res.ok) {
    let detail = "";
    try { const j = await res.json(); detail = typeof j.detail === "string" ? j.detail : ""; } catch (e) {}
    throw new ApiError(res.status, detail && res.status !== 422 ? detail : (FRIENDLY[res.status] || FRIENDLY[500]));
  }
  const type = res.headers.get("content-type") || "";
  return type.includes("json") ? res.json() : res.text();
}

export const health = () => request("/health", { timeout: 60000 });
