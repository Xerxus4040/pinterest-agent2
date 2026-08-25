const $ = (s) => document.querySelector(s);

function message(text, bad=false) {
  const el = $("#message");
  el.textContent = text;
  el.classList.remove("hidden");
  el.style.background = bad ? "#fee4e2" : "#e8f7ee";
  el.style.color = bad ? "#b42318" : "#067647";
}

async function api(url, options={}) {
  let response;
  try {
    response = await fetch(url, {
      credentials: "same-origin",
      ...options,
      headers: {"Content-Type":"application/json", ...(options.headers||{})}
    });
  } catch (e) {
    throw new Error("Backend Server Unreachable");
  }
  let data = {};
  try { data = await response.json(); } catch {}
  if (!response.ok) {
    const detail = data.body || data.error || response.statusText;
    throw new Error(`${detail}${data.status_code ? ` (HTTP ${data.status_code})` : ""}`);
  }
  return data;
}

async function load() {
  try {
    const me = await api("/api/me");
    $("#login").classList.add("hidden");
    $("#dashboard").classList.remove("hidden");
    $("#logout").classList.remove("hidden");
    $("#googleStatus").innerHTML = me.google_connected ? "Google Drive: <b class='ok'>Connected</b>" : "Google Drive: <b class='bad'>Not connected</b>";
    $("#pinterestStatus").innerHTML = me.pinterest_connected ? "Pinterest: <b class='ok'>Connected</b>" : `Pinterest: <a href="/oauth/pinterest">Connect Pinterest</a>`;
    if (me.drive_folder) {
      $("#folder").value = me.drive_folder.id;
      $("#folderInfo").textContent = me.drive_folder.name;
    }
    if (me.board) $("#boardInfo").textContent = `Selected: ${me.board.name || me.board.id}`;
    $("#automationState").textContent = me.paused ? "Paused" : "Running";
    await refreshJobs();
    if (me.pinterest_connected) await loadBoards();
  } catch (e) {
    $("#login").classList.remove("hidden");
    $("#dashboard").classList.add("hidden");
  }
}

async function loadBoards() {
  try {
    const data = await api("/api/pinterest/boards");
    const select = $("#boards");
    select.innerHTML = "";
    for (const b of data.items) {
      const option = document.createElement("option");
      option.value = b.id;
      option.textContent = b.name || b.id;
      option.dataset.name = b.name || "";
      select.appendChild(option);
    }
  } catch (e) {
    message(e.message, true);
  }
}

async function refreshJobs() {
  try {
    const data = await api("/api/jobs");
    $("#jobs").innerHTML = data.items.length ? data.items.map(j => `
      <div class="job">
        <b>${escapeHtml(j.file_name || "asset")}</b>
        <span class="badge">${escapeHtml(j.status)}</span>
        ${j.pinterest_pin_id ? `<span class="muted">Pin: ${escapeHtml(j.pinterest_pin_id)}</span>` : ""}
        ${j.title ? `<div>${escapeHtml(j.title)}</div>` : ""}
        ${j.error ? `<div class="error">${escapeHtml(j.error)}</div>` : ""}
      </div>
    `).join("") : "<p class='muted'>No jobs yet.</p>";
  } catch {}
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, c => ({
    "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"
  }[c]));
}

$("#saveFolder").onclick = async () => {
  try {
    const data = await api("/api/drive/folder", {
      method:"POST",
      body:JSON.stringify({folder:$("#folder").value})
    });
    $("#folderInfo").textContent = data.folder.name;
    message("Drive folder verified.");
  } catch(e) { message(e.message, true); }
};

$("#loadBoards").onclick = loadBoards;

$("#boards").onchange = async () => {
  const option = $("#boards").selectedOptions[0];
  if (!option) return;
  try {
    await api("/api/pinterest/board", {
      method:"POST",
      body:JSON.stringify({id:option.value,name:option.dataset.name})
    });
    $("#boardInfo").textContent = `Selected: ${option.textContent}`;
  } catch(e) { message(e.message, true); }
};

$("#scan").onclick = async () => {
  try {
    const data = await api("/api/drive/scan", {method:"POST"});
    message(`Drive scan complete. Found ${data.found} images; ${data.new_jobs} new jobs.`);
    await refreshJobs();
  } catch(e) { message(e.message, true); }
};

$("#start").onclick = async () => {
  try {
    await api("/api/automation/start", {method:"POST"});
    $("#automationState").textContent = "Running";
    message("Automation started.");
  } catch(e) { message(e.message, true); }
};

$("#pause").onclick = async () => {
  try {
    await api("/api/automation/pause", {method:"POST"});
    $("#automationState").textContent = "Paused";
    message("Automation paused.");
  } catch(e) { message(e.message, true); }
};

$("#process").onclick = async () => {
  try {
    message("Processing one job...");
    const data = await api("/api/automation/process-one", {method:"POST"});
    message(data.status === "failed" ? data.error : JSON.stringify(data));
    await refreshJobs();
  } catch(e) { message(e.message, true); }
};

$("#logout").onclick = async () => {
  await api("/api/logout", {method:"POST"});
  location.reload();
};

load();
setInterval(refreshJobs, 10000);
