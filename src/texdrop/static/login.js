"use strict";

document.getElementById("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = document.getElementById("login-error");
  error.textContent = "";
  const response = await fetch("/api/session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: document.getElementById("token").value.trim() }),
  });
  if (response.ok) {
    location.reload();
  } else {
    error.textContent = response.status === 401 ? "Invalid token." : `Login failed (${response.status}).`;
  }
});
