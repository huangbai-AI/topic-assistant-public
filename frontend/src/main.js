const DEFAULT_API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000";

function normalizeApiBase(value) {
  return String(value || "").trim().replace(/\/+$/, "") || DEFAULT_API_BASE;
}

const query = new URLSearchParams(window.location.search);
const fromQuery = query.get("apiBase");
const fromStorage = localStorage.getItem("ta_api_base");
const apiBase = normalizeApiBase(fromQuery || fromStorage || DEFAULT_API_BASE);

localStorage.setItem("ta_api_base", apiBase);

const target = `${apiBase}/dashboard`;
if (window.location.href !== target) {
  window.location.replace(target);
}
