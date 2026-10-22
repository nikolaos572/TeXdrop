"use strict";

// Shared by the pages behind the login, loaded before their own script: API calls and the header.

const $ = (id) => document.getElementById(id);

// Every request carries the cookie; the extra header marks it as coming from this page.
async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { "X-Requested-With": "fetch" } });
  if (response.status === 401) {
    location.reload(); // token revoked or cookie expired: back to the login page
    throw new Error("Session expired.");
  }
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      // keep the status text
    }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
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
