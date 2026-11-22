"use strict";

function total(map) {
  return Object.values(map || {}).reduce((sum, n) => sum + n, 0);
}
function sparkline(values) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 100 28");
  svg.setAttribute("class", "spark");
  svg.setAttribute("aria-hidden", "true");
  const clean = values.filter(v => typeof v === "number");
  if (clean.length < 2) return svg;
  const min = Math.min(...clean), max = Math.max(...clean), span = max - min || 1;
  const points = clean.map((v, i) => (i / (clean.length - 1)) * 100 + "," + (26 - ((v - min) / span) * 24)).join(" ");
  const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
  line.setAttribute("points", points);
  svg.append(line);
  return svg;
}
function loopStep(css, label, title, lines, extra) {
  return el("li", "loop-step " + css, el("small", "label", node("i", null, "dot"), label), node("strong", title),
    ...lines.map(text => node("span", text)), extra || null);
}
function topLines(map, names, limit = 3) {
  return Object.entries(map || {}).slice(0, limit).map(([key, n]) => count.format(n) + " " + (names[key] || key.replaceAll(/[._]/g, " ")));
}
function renderLoop(view) {
  const learning = view.learning;
  const last = learning?.generations.at(-1);
  byId("env-loop").replaceChildren(
    loopStep("observe", "Observe", count.format(total(view.observations)) + " signals",
      topLines(view.observations, { "file.changed": "file changes", "mail.received": "emails received", tick: "clock ticks", settled: "settled rounds" })),
    loopStep("post", "State", count.format(total(view.state)) + " posts",
      topLines(view.state, { claim: "values read", fact: "values confirmed", forecast: "forecasts" })),
    loopStep("act", "Act", count.format(total(view.actions)) + " actions",
      total(view.actions) ? topLines(view.actions, { executed: "done", needs_approval: "waiting for a person", blocked: "blocked by the gate" })
        : ["Every action passes the gate first"]),
    loopStep("reward", "Learn", learning ? "Generation " + last.generation : "No training run",
      learning ? ["Best score " + (last.train_best ?? 0).toFixed(2), count.format(last.violations || 0) + " rule violations"]
        : ["Reward is euros shipped, minus spend and penalties"],
      learning ? sparkline(learning.generations.map(g => g.train_best)) : null));
}
const envFields = {
  evidence: ["Verify evidence", "What do the records, documents, or replies establish?"],
  production: ["Plan production", "Which shortage limits packing, when will it bind, and how many units could a fix save?"],
  suppliers: ["Coordinate suppliers", "What quantity, arrival date, or delivery obligation needs confirmation?"],
  logistics: ["Coordinate logistics", "Which physical handoffs must happen together for the goods to arrive?"],
  quality: ["Contain quality risk", "Which stock needs quarantine, inspection, or a concession?"],
  purchasing: ["Evaluate purchases", "Which offer is worth its cost given reliability, timing, and production impact?"],
  approval: ["Route approvals", "Who has authority to approve the proposed spend, exception, or release?"],
};
const envIssues = {
  booking_error: ["Stock booking error", "The receipt overstates the quantity available to pack."],
  short_shipment: ["Short shipment", "The shipment carries fewer units than the booking shows."],
  failed_lot: ["Failed inspection", "A failed batch still appears available for packing."],
  stuck_at_port: ["Shipment stuck at port", "The cargo needs both a pickup and a receiving slot."],
  terms_hold: ["Delivery terms conflict", "Conflicting delivery terms are holding up the shipment."],
  rival_offers: ["Competing supplier offers", "Two offers differ in price and delivery reliability."],
  decoy: ["Unconfirmed stock alert", "The plan flags a part as short, but nothing is wrong."],
};
const envActions = {
  request_pickup: ["logistics", "Request pickup"], request_dock_slot: ["logistics", "Book receiving slot"],
  book_freight: ["logistics", "Book freight"], confirm_terms: ["suppliers", "Confirm delivery terms"],
  accept_offer: ["purchasing", "Accept supplier offer"], quarantine_batch: ["quality", "Quarantine batch"],
  request_concession: ["approval", "Request quality concession"], set_priority: ["production", "Change packing priority"],
  data_fix_request: ["evidence", "Request record correction"],
};
// The replay. A recorded Gym scenario is a list of events; each one is shown in three panels: what happened
// (the data the agents could see), how the agents reacted (the actions they scheduled, or why none), and what
// the episode paid with and without agents. A last step sums the scenario up.
const compactEuros = new Intl.NumberFormat(undefined, { style: "currency", currency: "EUR", notation: "compact", maximumFractionDigits: 2 });
const BOOK_ROWS = new Set(["ERP stock", "Booked", "ERP status", "Tracker", "Terms on the PO", "Plan flag"]);
const REAL_ROWS = new Set(["Real stock", "Actually shipped", "Inspection", "Reality", "Terms in the contract"]);
const STEP_MS = 6500;
let envRecords = [];
let envStep = 0;
let envRun = 0;
let envPaused = false;
let envTimer;

function envRecord() {
  return envRecords[byId("env-scenario").selectedIndex];
}
function envTitle(event) {
  return (envIssues[event.type] || [roleName(event.type)])[0];
}
function envOutcome(event) {
  if (event.decoy) return ["decoy", "False alarm, left alone"];
  if (event.responses.length) return ["handled", "Handled"];
  return ["missed", "No action"];
}
function signed(value) {
  return (value >= 0 ? "+" : "") + compactEuros.format(value);
}

function renderWhat(record, event) {
  if (!event) {
    return [node("h3", "The scenario, settled", "env-what-title"),
      node("p", record.events.length + " events reached the agents in " + count.format(record.hours) +
        " hours to the packing deadline.", "env-text"),
      el("ul", "env-summary", ...record.events.map(e => {
        const [css, text] = envOutcome(e);
        return el("li", css, node("span", envTitle(e)), node("small", text));
      }))];
  }
  const description = envIssues[event.type]?.[1] || event.problem;
  return [node("h3", envTitle(event), "env-what-title"), node("p", description, "env-text"),
    el("dl", "env-facts", ...event.facts.map(([label, value]) =>
      el("div", BOOK_ROWS.has(label) ? "book" : REAL_ROWS.has(label) ? "real" : "", node("dt", label), node("dd", value))))];
}
function renderHow(record, event) {
  if (!event) {
    const acts = record.events.flatMap(e => e.responses);
    const byAgent = {};
    for (const r of acts) byAgent[r.agent] = (byAgent[r.agent] || 0) + 1;
    const handled = record.events.filter(e => !e.decoy && e.responses.length).length;
    const real = record.events.filter(e => !e.decoy).length;
    return [node("p", handled + " of " + real + " real problems handled with " + acts.length +
        (acts.length === 1 ? " action." : " actions."), "env-text strong"),
      el("ul", "env-decisions", ...Object.entries(byAgent).map(([agent, n]) =>
        el("li", "", node("strong", roleName(agent)), node("span", n + (n === 1 ? " action" : " actions")))))];
  }
  if (!event.responses.length) {
    return [el("div", "env-none " + (event.decoy ? "decoy" : "missed"),
      node("strong", event.decoy ? "No action, correctly" : "No action"), node("span", event.why_not || ""))];
  }
  return [el("ul", "env-decisions", ...event.responses.map(r => {
    const [field, label] = envActions[r.action] || ["", roleName(r.action)];
    return el("li", "", node("strong", roleName(r.agent)), node("span", label),
      node("small", [envFields[field]?.[0], "hour " + count.format(r.hour), r.cost_eur ? euros.format(r.cost_eur) : ""]
        .filter(Boolean).join(" · ")));
  }))];
}
function renderPay(record, event) {
  const top = Math.max(record.idle, record.reward, 1);
  const bar = (label, value, css) => el("div", "env-bar " + css, el("div", "env-bar-label", node("span", label),
    node("strong", compactEuros.format(value))), el("div", "env-bar-track", Object.assign(node("i"), {
    style: "width:" + Math.max(0, value / top * 100).toFixed(1) + "%" })));
  const gain = record.reward - record.idle;
  const out = [bar("Without agents", record.idle, "idle"), bar("With agents", record.reward, "agents"),
    el("p", "env-gain" + (gain < 0 ? " loss" : ""), node("strong", signed(gain)), node("span", " from the agents")),
    node("p", count.format(record.packed) + " copies packed by the deadline.", "env-note")];
  if (event) {
    const [css] = envOutcome(event);
    out.push(node("p", css === "handled" ? "This event's actions are part of the agents' reward."
      : css === "decoy" ? "Acting here would only have cost money." : "This event adds nothing to the agents' reward.",
    "env-event-pay " + css));
  }
  return out;
}
function renderSteps(record) {
  const steps = [...record.events.map(envTitle), "Result"];
  byId("env-events").replaceChildren(...steps.map((title, index) => {
    const event = record.events[index];
    const chip = node("button", (index + 1) + ". " + title, "env-chip " + (event ? envOutcome(event)[0] : "result") +
      (index === envStep ? " current" : ""));
    chip.type = "button";
    chip.setAttribute("aria-current", index === envStep ? "step" : "false");
    chip.onclick = () => showStep(index, true);
    return chip;
  }));
}
function showStep(index, byHand = false) {
  const record = envRecord();
  if (!record) return;
  envStep = Math.max(0, Math.min(index, record.events.length));
  const event = record.events[envStep];
  byId("env-what").replaceChildren(...renderWhat(record, event));
  byId("env-how").replaceChildren(...renderHow(record, event));
  byId("env-pay").replaceChildren(...renderPay(record, event));
  byId("env-position").textContent = event ? "Event " + (envStep + 1) + " of " + record.events.length : "Result";
  byId("env-prev").disabled = envStep === 0;
  byId("env-next").disabled = envStep === record.events.length;
  renderSteps(record);
  const stage = byId("env-cols");
  stage.classList.remove("reveal");
  stage.getBoundingClientRect();
  stage.classList.add("reveal");
  if (byHand) setPaused(true);
  else schedule();
}
function schedule() {
  clearTimeout(envTimer);
  const run = envRun;
  if (envPaused || envStep >= envRecord().events.length) {
    if (!envPaused) setPaused(true);
    return;
  }
  envTimer = setTimeout(() => { if (run === envRun && !envPaused) showStep(envStep + 1); }, STEP_MS);
}
function setPaused(paused) {
  envPaused = paused;
  clearTimeout(envTimer);
  const atEnd = envStep >= (envRecord()?.events.length ?? 0);
  byId("env-play").textContent = !paused ? "Pause" : atEnd ? "Replay scenario" : "Play";
}
function togglePlay() {
  if (!envPaused) { setPaused(true); return; }
  envPaused = false;
  if (envStep >= envRecord().events.length) { showStep(0); return; }
  setPaused(false);
  schedule();
}
function playScenario() {
  envRun++;
  const record = envRecord();
  byId("env-replay-meta").textContent = "Offline Gym · " + record.events.length + " events · " +
    count.format(record.hours) + " h to the deadline";
  envPaused = matchMedia("(prefers-reduced-motion: reduce)").matches;
  setPaused(envPaused);
  showStep(0);
}
function stopEnvPlayback() {
  envRun++;
  setPaused(true);
}

// Open on the scenario where the agents gained the most over doing nothing: it shows the most.
function defaultScenario(records) {
  const gain = r => r.reward - r.idle;
  return records.reduce((best, r, i) => gain(r) > gain(records[best]) ? i : best, 0);
}

async function loadEnvReplays() {
  if (envRecords.length) { playScenario(); return; }
  try {
    envRecords = await readJson("/environment-replays.json");
    byId("env-scenario").replaceChildren(...envRecords.map(r => new Option(
      r.id === "launch-replay" ? "Customer launch replay" : roleName(r.id.replaceAll("-", " ")))));
    byId("env-scenario").selectedIndex = defaultScenario(envRecords);
    byId("env-scenario").onchange = playScenario;
    byId("env-play").onclick = togglePlay;
    byId("env-prev").onclick = () => showStep(envStep - 1, true);
    byId("env-next").onclick = () => showStep(envStep + 1, true);
    byId("env-scenario").disabled = byId("env-play").disabled = false;
    playScenario();
  } catch {
    byId("env-what").replaceChildren(node("p", "The scenario recordings could not be loaded.", "env-text"));
    byId("env-scenario").replaceChildren(new Option("Recordings unavailable"));
  }
}

async function loadEnvironment() {
  loadEnvReplays();
  try {
    const view = await readJson("/api/environment");
    byId("env-meta").textContent = view.agents.length + " registered agents";
    renderLoop(view);
    byId("env-agents").replaceChildren(...view.agents.map(a =>
      el("div", "", node("strong", roleName(a.name)), node("small", roleName(a.lane) + " · " + count.format(a.posts) + " posts"),
        node("span", a.subscribes.map(s => s.replace(":", " ")).join(", ")))));
  } catch {
    byId("env-meta").textContent = "";
    byId("env-loop").replaceChildren(node("li", "Live Board activity is unavailable.", "empty"));
  }
}
window.addEventListener("hashchange", () => {
  if (location.hash !== "#environment") stopEnvPlayback();
});
