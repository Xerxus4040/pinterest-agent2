const BACKEND = "https://YOUR-DOMAIN.vercel.app";

const $ = id => document.getElementById(id);
function log(text, bad=false) {
  const el = $("log");
  el.textContent = `[${new Date().toLocaleTimeString()}] ${text}\n` + el.textContent;
  el.className = bad ? "bad" : "";
}
async function api(path, options={}) {
  try {
    const r = await fetch(BACKEND + path, {
      credentials:"include",
      ...options,
      headers:{"Content-Type":"application/json", ...(options.headers||{})}
    });
    let data={}; try {data=await r.json()} catch {}
    if (!r.ok) throw new Error(data.error || data.body || `HTTP ${r.status}`);
    return data;
  } catch(e) {
    if (String(e.message).includes("Failed to fetch")) throw new Error("Backend Server Unreachable");
    throw e;
  }
}
async function refresh() {
  try {
    const me=await api("/api/me");
    $("backend").textContent="Online"; $("backend").className="ok";
    $("drive").textContent=me.google_connected?"Connected":"Not connected";
    $("pinterest").textContent=me.pinterest_connected?"Connected":"Not connected";
    $("automation").textContent=me.paused?"Paused":"Running";
  } catch(e) {
    $("backend").textContent="Unreachable"; $("backend").className="bad";
    log(e.message,true);
  }
}
$("start").onclick=async()=>{try{await api("/api/automation/start",{method:"POST"});log("Automation started");refresh()}catch(e){log(e.message,true)}};
$("pause").onclick=async()=>{try{await api("/api/automation/pause",{method:"POST"});log("Automation paused");refresh()}catch(e){log(e.message,true)}};
$("process").onclick=async()=>{try{const x=await api("/api/automation/process-one",{method:"POST"});log(JSON.stringify(x,null,2));refresh()}catch(e){log(e.message,true)}};
refresh();
