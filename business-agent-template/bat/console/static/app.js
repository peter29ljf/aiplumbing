"use strict";

// One page, three views, one open connection. The build runs for minutes at a time, so
// everything here is written around watching rather than around asking: the log is fed by
// server-sent events, and the tabs re-read state when you switch to them.

const $ = (id) => document.getElementById(id);
const get = (url) => fetch(url).then((r) => r.json());
const post = (url, body) =>
  fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  }).then((r) => r.json());

let current = "";
let stream = null;

// ---- the log ----------------------------------------------------------

function line(text, kind) {
  const log = $("log");
  const stuck = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
  const p = document.createElement("p");
  p.className = "line " + (kind || "");
  p.textContent = text;
  log.appendChild(p);
  if (stuck) log.scrollTop = log.scrollHeight;   // only if they were already at the end
}

// What one Claude Code event means to somebody watching. Tool calls are the interesting
// part of a long silence — "writing rules/greeting.md" beats four minutes of nothing.
function show(event) {
  if (event.type === "assistant") {
    for (const block of (event.message && event.message.content) || []) {
      if (block.type === "text" && block.text.trim()) line(block.text.trim());
      if (block.type === "tool_use") {
        const what = block.input && (block.input.file_path || block.input.command ||
                                     block.input.pattern || "");
        line(`${block.name}  ${String(what).slice(0, 120)}`, "tool");
      }
    }
  } else if (event.type === "bat.started") {
    line(`— ${event.phase} —`, "note");
  } else if (event.type === "bat.finished") {
    line(event.ok ? `— done (${(event.usd || 0).toFixed(4)} USD) —`
                  : `— failed: ${event.error} —`, event.ok ? "note" : "bad");
    refresh();
  } else if (event.type === "bat.stopped") {
    line("— stopped —", "bad");
    refresh();
  } else if (event.type === "bat.crashed") {
    line(event.trace, "bad");
    refresh();
  } else if (event.type === "bat.stderr") {
    line(event.text, "bad");
  }
}

function listen(name) {
  if (stream) stream.close();
  stream = new EventSource(`/api/build/${name}/events`);
  stream.onmessage = (e) => show(JSON.parse(e.data));
}

// ---- state ------------------------------------------------------------

async function refresh() {
  if (!current) return;
  const state = await get(`/api/build/${current}`);

  const phase = $("phase");
  phase.textContent = state.busy ? `${state.phase} · working`
                                 : `${state.phase}${state.waiting ? " · " + state.waiting : ""}`;
  phase.className = "pill" + (state.busy ? " busy" : state.waiting ? " waiting" : "");

  const waiting = $("waiting");
  waiting.classList.toggle("hidden", !state.waiting);
  $("waiting-note").textContent = state.note || state.waiting || "";
  $("approve").classList.toggle("hidden", state.waiting !== "waiting for the plan to be approved");
  $("resume").classList.toggle("hidden", state.waiting !== "paused");

  $("send").disabled = state.busy;
  $("pause").disabled = !state.busy;

  const s = state.spend || {};
  $("spend").textContent =
    `${(s.usd || 0).toFixed(4)} USD · ${s.calls || 0} calls · cache ${Math.round((s.cache_hit_rate || 0) * 100)}%`;
}

async function openProject(name) {
  current = name;
  $("log").innerHTML = "";
  const state = await get(`/api/build/${name}`);
  for (const row of state.transcript || []) {
    if (row.said) line(row.said, "you");
    if (row.replied) line(row.replied);
  }
  if (state.plan && state.waiting) line(state.plan, "note");
  listen(name);
  refresh();
  loadTab();
}

async function loadProjects(pick) {
  const { projects } = await get("/api/projects");
  const select = $("project");
  select.innerHTML = "";
  for (const p of projects) {
    const option = document.createElement("option");
    option.value = p.name;
    option.textContent = `${p.name} · ${p.phase}${p.nodes ? ` · ${p.nodes} nodes` : ""}`;
    select.appendChild(option);
  }
  const name = pick || (projects[0] && projects[0].name);
  if (name) { select.value = name; openProject(name); }
}

// ---- flow -------------------------------------------------------------

async function loadFlow() {
  const data = await get(`/api/flow/${current}`);
  const body = $("nodes").querySelector("tbody");
  body.innerHTML = "";
  if (data.error) {
    body.innerHTML = `<tr><td colspan="8" class="lost">${data.error}</td></tr>`;
    return;
  }
  for (const n of data.nodes) {
    const leads = n.terminal ? "— ends —"
      : n.next ? `→ ${n.next}`
      : Object.entries(n.branch).map(([k, v]) => `${k} → ${v}`).join("\n");
    const row = document.createElement("tr");
    // 8,000 is the ceiling the engine tests hold. Anything near it is worth a look.
    if (n.total > 7000) row.className = "fat";
    row.innerHTML = `
      <td><code>${n.name}</code>${n.name === data.entry ? " <span class='muted'>entry</span>" : ""}</td>
      <td>${n.goal}</td>
      <td><code>${n.rules.join(", ")}</code></td>
      <td><code>${n.tools.join("\n")}</code></td>
      <td class="num">${n.prompt.toLocaleString()}</td>
      <td class="num">${n.schemas.toLocaleString()}</td>
      <td class="num">${n.total.toLocaleString()}</td>
      <td><code>${leads}</code></td>`;
    body.appendChild(row);
  }
}

// ---- dashboard --------------------------------------------------------

async function loadDash() {
  const d = await get(`/api/dashboard/${current}`);
  $("rate").textContent = d.runs ? `${Math.round(d.rate * 100)}%` : "—";
  $("counts").textContent = d.runs ? `${d.passed}/${d.runs}` : "no runs yet";
  $("builder-usd").textContent = `${(d.builder.usd || 0).toFixed(3)}`;
  $("agent-cache").textContent = d.agent.prompt
    ? `${Math.round(d.agent.cache_hit_rate * 100)}%` : "—";

  const scenarios = $("scenarios").querySelector("tbody");
  scenarios.innerHTML = d.scenarios.map((s) => {
    const clean = s.won === s.of;
    const verdict = clean ? "PASS" : s.won ? "FLAKY" : "FAIL";
    return `<tr><td class="${clean ? "won" : "lost"}">${verdict}</td>
            <td><code>${s.id}</code></td><td class="num">${s.won}/${s.of}</td></tr>`;
  }).join("") || `<tr><td class="muted">nothing has been run yet</td></tr>`;

  const means = {
    config: "ours — a rules file and a tool list contradicting each other",
    model: "it had the tool and the instruction and did otherwise",
    harness: "the scenario or the runner, not the agent",
    unclear: "not decidable from what was recorded",
  };
  const faults = $("faults").querySelector("tbody");
  faults.innerHTML = Object.entries(d.faults).map(([kind, n]) =>
    `<tr><td class="${kind === "config" ? "lost" : ""}">${kind}</td>
     <td class="num">${n}</td><td class="muted">${means[kind] || ""}</td></tr>`
  ).join("") || `<tr><td class="won">none</td></tr>`;

  const timings = $("timings").querySelector("tbody");
  timings.innerHTML = d.timings.map((t) =>
    `<tr class="${t.over ? "slow" : ""}"><td><code>${t.node}</code></td>
     <td class="num">${t.worst}s</td><td class="num">${t.mean}s</td>
     <td class="num">${t.calls}</td><td class="num">${t.over || ""}</td></tr>`
  ).join("") || `<tr><td class="muted">nothing has been run yet</td></tr>`;
}

function loadTab() {
  const open = document.querySelector("nav button.on").dataset.tab;
  if (open === "flow") loadFlow();
  if (open === "dash") loadDash();
}

// ---- wiring -----------------------------------------------------------

document.querySelectorAll("nav button").forEach((tab) => {
  tab.onclick = () => {
    document.querySelectorAll("nav button").forEach((b) => b.classList.remove("on"));
    document.querySelectorAll(".tab").forEach((s) => s.classList.remove("on"));
    tab.classList.add("on");
    $(tab.dataset.tab).classList.add("on");
    loadTab();
  };
});

$("project").onchange = (e) => openProject(e.target.value);

$("new").onclick = async () => {
  const name = prompt("Name for the new agent (e.g. dental, travel, takeaway)");
  if (!name) return;
  const made = await post("/api/projects", { name });
  if (made.error) return alert(made.error);
  await loadProjects(made.name);
};

$("say").onsubmit = async (e) => {
  e.preventDefault();
  const text = $("text").value.trim();
  if (!text || !current) return;
  line(text, "you");
  $("text").value = "";
  const started = await post(`/api/build/${current}/say`, { text });
  if (started.error) line(started.error, "bad");
  refresh();
};

$("text").onkeydown = (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("say").requestSubmit();
};

$("pause").onclick = async () => { await post(`/api/pause/${current}`); refresh(); };
$("resume").onclick = async () => { await post(`/api/resume/${current}`); refresh(); };
$("approve").onclick = async () => {
  await post(`/api/approve/${current}`);
  line("— plan approved —", "note");
  refresh();
};

loadProjects();
setInterval(refresh, 5000);
