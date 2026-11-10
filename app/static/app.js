"use strict";

// The compile page; common.js provides $, api, showMessage, formatSize and the header.

const PAGE_SIZE = 5;
const state = { file: null, texFiles: [], refreshTimer: null, page: 1, loads: 0, rows: null };
const shownError = { hint: null, hideTimer: null }; // the "?" whose error is open

// The same rule the server applies: the main file must be one of the archive's .tex files.
function updateCompileButton() {
  $("compile-button").disabled = !(state.file && state.texFiles.includes($("main").value.trim()));
}

function resetForm() {
  state.file = null;
  state.texFiles = [];
  $("file").value = "";
  $("main").value = "";
  $("main").disabled = true;
  $("tex-files").replaceChildren();
  $("drop-title").textContent = "Drop your LaTeX project here";
  $("drop-hint").textContent = "or click to choose a .zip, .7z, .tar, .tar.gz, .tar.bz2 or .tar.xz file";
  updateCompileButton();
}

async function selectFile(file) {
  resetForm();
  state.file = file;
  $("drop-title").textContent = file.name;
  $("drop-hint").textContent = `${formatSize(file.size)} · click or drop to choose another file`;
  showMessage("Inspecting the archive…");
  const form = new FormData();
  form.append("file", file);
  try {
    const info = await api("/api/inspect", { method: "POST", body: form });
    if (state.file !== file) return; // another file was chosen meanwhile
    state.texFiles = info.tex_files;
    $("tex-files").replaceChildren(...info.tex_files.map((name) => new Option(name, name)));
    if (info.tex_files.length === 0) {
      showMessage("No .tex file found in this archive.", "error");
      return;
    }
    $("main").disabled = false;
    $("main").value = info.main || "";
    showMessage(
      info.main
        ? `Main file: ${info.main}`
        : `${info.tex_files.length} .tex files found: type or pick the main file.`,
    );
    updateCompileButton();
    if (!info.main) $("main").focus();
  } catch (error) {
    if (state.file === file) showMessage(error.message, "error");
  }
}

async function compile() {
  if ($("compile-button").disabled) return;
  $("compile-button").disabled = true;
  const form = new FormData();
  form.append("file", state.file);
  form.append("main", $("main").value.trim());
  form.append("engine", $("engine").value);
  showMessage("Uploading…");
  try {
    const job = await api("/api/jobs", { method: "POST", body: form });
    resetForm();
    showMessage(`Job #${job.id} queued.`, "ok");
    state.page = 1; // where the new job is listed
    await loadJobs();
  } catch (error) {
    showMessage(error.message, "error");
    updateCompileButton();
  }
}

function cell(content, className = "") {
  const td = document.createElement("td");
  if (content instanceof Node) td.append(content);
  else td.textContent = content ?? "";
  td.className = className;
  return td;
}

function link(href, text, download) {
  const a = document.createElement("a");
  a.href = href;
  a.textContent = text;
  if (download) a.download = "";
  else a.target = "_blank";
  a.rel = "noopener";
  return a;
}

function formatTime(iso) {
  return iso ? iso.slice(0, 19).replace("T", " ") : "";
}

// A failed job's "?": its error is shown over the page while the pointer is on it (or it
// has the focus), so that long errors do not make the rows taller.
function errorHint(error) {
  const hint = document.createElement("button");
  hint.type = "button";
  hint.className = "error-hint";
  hint.textContent = "?";
  hint.setAttribute("aria-label", `Error: ${error}`);
  hint.addEventListener("mouseenter", () => showError(hint, error));
  hint.addEventListener("focus", () => showError(hint, error));
  hint.addEventListener("mouseleave", hideErrorSoon);
  hint.addEventListener("blur", hideErrorSoon);
  return hint;
}

function showError(hint, error) {
  clearTimeout(shownError.hideTimer);
  shownError.hint = hint;
  $("error-tooltip").textContent = error;
  $("error-tooltip").hidden = false;
  placeError();
}

// Below its "?", or above it if there is no room, and always inside the window.
function placeError() {
  const tooltip = $("error-tooltip");
  tooltip.style.left = tooltip.style.top = "0"; // measured where it has the whole width
  const anchor = shownError.hint.getBoundingClientRect();
  const { clientWidth, clientHeight } = document.documentElement;
  const below = anchor.bottom + 6;
  const top = below + tooltip.offsetHeight <= clientHeight - 8 ? below : anchor.top - 6 - tooltip.offsetHeight;
  tooltip.style.top = `${Math.max(8, top)}px`;
  tooltip.style.left = `${Math.max(8, Math.min(anchor.left, clientWidth - 8 - tooltip.offsetWidth))}px`;
}

function hideError() {
  clearTimeout(shownError.hideTimer);
  $("error-tooltip").hidden = true;
}

// After a moment, to let the pointer move onto the error (to select or scroll it); not while the
// pointer is on the error or its "?" (clicking into the error takes the focus from the "?").
function hideErrorSoon() {
  shownError.hideTimer = setTimeout(() => {
    if (!$("error-tooltip").matches(":hover") && !shownError.hint.matches(":hover")) hideError();
  }, 200);
}

function jobRow(job) {
  const status = document.createElement("span");
  status.className = `badge ${job.status}`;
  status.textContent = job.status === "queued" ? `queued · #${job.queue_position} in line` : job.status;
  const statusCell = cell(status, "nowrap");
  if (job.error) statusCell.append(errorHint(job.error));

  const downloads = document.createElement("div");
  downloads.className = "links";
  downloads.append(link(`/api/jobs/${job.id}/input`, "Archive", true));
  if (job.has_pdf) downloads.append(link(`/api/jobs/${job.id}/pdf`, "PDF"));
  if (job.has_log) downloads.append(link(`/api/jobs/${job.id}/log`, "Log"));

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "danger small";
  remove.textContent = "Delete";
  remove.addEventListener("click", () => deleteJob(job));

  const row = document.createElement("tr");
  row.append(
    cell(String(job.id), "nowrap"),
    cell(job.filename),
    cell(job.main),
    cell(job.engine, "nowrap"),
    statusCell,
    cell(formatTime(job.submitted_at), "nowrap"),
    cell(formatTime(job.finished_at), "nowrap"),
    cell(downloads, "nowrap"),
    cell(remove, "nowrap"),
  );
  return row;
}

async function loadJobs() {
  clearTimeout(state.refreshTimer);
  const load = ++state.loads;
  let active = false;
  try {
    const list = await api(`/api/jobs?page=${state.page}&per_page=${PAGE_SIZE}`);
    if (load !== state.loads) return; // a newer load (of another page, say) shows its own result
    const pages = Math.max(1, Math.ceil(list.total / PAGE_SIZE));
    if (state.page > pages) {
      state.page = pages; // this page has emptied (jobs were deleted): show the last one
      return loadJobs();
    }
    const rows = JSON.stringify(list.jobs);
    if (rows !== state.rows) {
      // Only when it changed: the list is reloaded every few seconds, and an open error stays open.
      state.rows = rows;
      hideError();
      $("jobs-body").replaceChildren(...list.jobs.map(jobRow));
    }
    $("jobs-table").hidden = list.total === 0;
    $("no-jobs").hidden = list.total > 0;
    $("delete-all").disabled = list.total === 0;
    $("pager").hidden = pages === 1;
    $("page-info").textContent = `Page ${state.page} of ${pages} · ${list.total} jobs`;
    $("previous-page").disabled = state.page === 1;
    $("next-page").disabled = state.page === pages;
    active = list.jobs.some((job) => job.status === "queued" || job.status === "processing");
  } catch (error) {
    if (load !== state.loads) return;
    showMessage(error.message, "error");
  }
  state.refreshTimer = setTimeout(loadJobs, active ? 2000 : 30000);
}

function goToPage(page) {
  state.page = Math.max(1, page);
  loadJobs();
}

async function deleteJob(job) {
  if (!confirm(`Delete job #${job.id} (${job.filename}) and its files?`)) return;
  try {
    await api(`/api/jobs/${job.id}`, { method: "DELETE" });
  } catch (error) {
    showMessage(error.message, "error");
  }
  await loadJobs();
}

async function deleteAllJobs() {
  if (!confirm("Delete ALL your jobs and their files, including queued and running ones?")) return;
  try {
    const result = await api("/api/jobs", { method: "DELETE" });
    showMessage(`Deleted ${result.deleted} job(s).`, "ok");
  } catch (error) {
    showMessage(error.message, "error");
  }
  await loadJobs();
}

function setUpDropzone() {
  const zone = $("dropzone");
  zone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      $("file").click();
    }
  });
  for (const type of ["dragenter", "dragover"]) {
    zone.addEventListener(type, (event) => {
      event.preventDefault();
      zone.classList.add("dragging");
    });
  }
  for (const type of ["dragleave", "drop"]) {
    zone.addEventListener(type, () => zone.classList.remove("dragging"));
  }
  zone.addEventListener("drop", (event) => {
    event.preventDefault();
    const [file] = event.dataTransfer.files;
    if (file) selectFile(file);
  });
  $("file").addEventListener("change", () => {
    const [file] = $("file").files;
    if (file) selectFile(file);
  });
}

setUpDropzone();
$("main").addEventListener("input", updateCompileButton);
$("main").addEventListener("keydown", (event) => {
  if (event.key === "Enter") compile();
});
$("compile-button").addEventListener("click", compile);
$("refresh").addEventListener("click", loadJobs);
$("previous-page").addEventListener("click", () => goToPage(state.page - 1));
$("next-page").addEventListener("click", () => goToPage(state.page + 1));
$("delete-all").addEventListener("click", deleteAllJobs);
$("error-tooltip").addEventListener("mouseenter", () => clearTimeout(shownError.hideTimer));
$("error-tooltip").addEventListener("mouseleave", hideErrorSoon);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") hideError();
});
// An open error follows its "?" when the page or the table scrolls (not when the error itself does).
addEventListener(
  "scroll",
  (event) => {
    if (!$("error-tooltip").hidden && event.target !== $("error-tooltip")) placeError();
  },
  { capture: true, passive: true },
);
loadJobs();
