"use strict";

// The read-only server info page; common.js provides $, api, showMessage, formatSize and the header.

function plural(count, word) {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

function limit(megabytes) {
  return megabytes ? `${megabytes} MB` : "No limit";
}

// The address to give an AI agent: the server's as this browser reaches it (behind a proxy, the proxy's).
function showMcp(server) {
  if (!server.mcp_path) {
    $("mcp").textContent = "Off (ENABLE_MCP=1 turns it on)";
    return;
  }
  const address = document.createElement("code");
  address.textContent = new URL(server.mcp_path, location.origin).href;
  const lifetime = plural(server.link_expiry_minutes, "minute");
  $("mcp").replaceChildren(address, `, with transfer links valid for ${lifetime}`);
}

async function loadInfo() {
  try {
    const { server, queue, account } = await api("/api/info");
    $("version").textContent = server.version;
    $("tex-live").textContent = server.tex_live ?? "unknown";
    $("engines").textContent = server.engines
      .map((engine) => (engine === server.default_engine ? `${engine} (default)` : engine))
      .join(", ");
    $("timeout").textContent = `${plural(server.compile_timeout_minutes, "minute")}, then the job fails`;
    $("max-upload").textContent = `${server.max_upload_mb} MB (at most ${server.max_extracted_mb} MB unpacked)`;
    const unpackTime = plural(server.unpack_timeout_minutes, "minute");
    $("unpack").textContent = server.unpack_memory_mb
      ? `${unpackTime} and ${server.unpack_memory_mb} MB`
      : `${unpackTime}, no memory limit`;
    $("compile-memory").textContent = limit(server.compile_memory_mb);
    $("compile-disk").textContent = limit(server.compile_disk_mb);
    $("auto-delete").textContent = server.auto_delete_days
      ? `${plural(server.auto_delete_days, "day")} after a job finishes`
      : "Never: jobs stay until you delete them";
    $("timezone").textContent = server.timezone;
    showMcp(server);
    $("queued").textContent = plural(queue.queued, "job");
    $("processing").textContent = plural(queue.processing, "job");
    $("account-user").textContent = account.user;
    $("account-jobs").textContent = String(account.jobs);
    $("storage").textContent = server.max_user_storage_mb
      ? `${formatSize(account.storage_bytes)} of ${server.max_user_storage_mb} MB`
      : `${formatSize(account.storage_bytes)} (no limit)`;
    $("message").hidden = true;
  } catch (error) {
    showMessage(error.message, "error");
    $("message").hidden = false;
  }
}

$("refresh").addEventListener("click", loadInfo);
loadInfo();
