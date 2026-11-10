"use strict";

// Shared by the pages behind the login, loaded before their own script: API calls and the header.

const $ = (id) => document.getElementById(id);

// Every request carries the cookie; the extra header marks it as coming from this page
// (the server refuses cookie-authenticated changes without it).
async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, { ...options, headers: { ...options.headers, "X-Requested-With": "fetch" } });
  } catch {
    throw new Error("The server cannot be reached.");
  }
  if (response.status === 401) {
    location.reload(); // logged out, session expired or token changed: back to the login page
    throw new Error("Session expired.");
  }
  if (!response.ok) throw new Error(await errorDetail(response));
  return response.status === 204 ? null : response.json();
}

// The server's reason ({"detail": ...}), or the status if the body has none.
async function errorDetail(response) {
  try {
    const { detail } = await response.json();
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map((item) => item.msg ?? JSON.stringify(item)).join("; ");
    return JSON.stringify(detail);
  } catch {
    return `${response.status} ${response.statusText}`;
  }
}

function showMessage(text, kind = "") {
  $("message").textContent = text;
  $("message").className = `message ${kind}`;
}

function formatSize(bytes) {
  const units = ["B", "KB", "MB", "GB"];
  let size = bytes;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

async function logout() {
  await api("/api/session", { method: "DELETE" });
  location.reload();
}

$("logout").addEventListener("click", logout);
api("/api/session").then((session) => {
  $("user").textContent = session.user;
});
