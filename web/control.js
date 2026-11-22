"use strict";

const byId = id => document.getElementById(id);
const count = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
const euros = new Intl.NumberFormat(undefined, { style: "currency", currency: "EUR", maximumFractionDigits: 0 });
let snapshot;
let stream;
let timer;
let refreshing = false;
let pending = false;
let updatingPause = false;
let evidenceRequest = 0;

function node(tag, text, css) {
  const element = document.createElement(tag);
  if (text != null) element.textContent = text;
  if (css) element.className = css;
  return element;
}
function notice(text) {
  byId("feedback").textContent = text;
  byId("feedback").hidden = !text;
}
function liveStatus() {
  const status = byId("connection");
  status.textContent = "Live";
  status.className = "live-status live";
}
function offline(text) {
  const status = byId("connection");
  status.textContent = text;
  status.className = "live-status warning";
}
function productName(id) {
  return snapshot?.products[id]?.name || id;
}
function roleName(role) {
  const text = role?.replaceAll("_", " ") || "";
  return text.charAt(0).toUpperCase() + text.slice(1);
}
function renderPause() {
  const button = byId("pause");
  button.disabled = updatingPause || !snapshot;
  button.textContent = snapshot?.paused ? "Resume" : "Pause all";
  button.setAttribute("aria-label", snapshot?.paused ? "Outgoing emails and orders are held. Resume" : "Hold every outgoing email and order");
  button.title = button.getAttribute("aria-label");
}
byId("pause").onclick = async () => {
  if (updatingPause || !snapshot) return;
  updatingPause = true;
  renderPause();
  try {
    const response = await fetch("/api/control/" + (snapshot.paused ? "resume" : "pause"), { method: "POST" });
    if (!response.ok) throw new Error();
    snapshot.paused = (await response.json()).paused;
    notice(snapshot.paused ? "Outgoing emails and orders are held until you resume." : "Outgoing emails and orders resumed.");
  } catch {
    notice("Could not update outgoing actions. Check the current state and try again.");
  } finally {
    updatingPause = false;
    renderPause();
    scheduleRefresh();
  }
};
function sourceButton(postId, product, label = "Source") {
  const button = node("button", label, "source-link");
  button.onclick = () => showSources(postId, product);
  return button;
}
async function showSources(postId, product) {
  const request = ++evidenceRequest;
  previewRequest?.abort();
  byId("excerpt").replaceChildren();
  const dialog = byId("evidence");
  if (!dialog.open) dialog.showModal();
  const sources = byId("sources");
  sources.textContent = "Loading sources…";
  const params = new URLSearchParams();
  if (postId) params.set("post_id", postId);
  if (product) params.set("product", product);
  try {
    const response = await fetch("/api/card-sources?" + params);
    if (!response.ok) throw new Error();
    const data = await response.json();
    if (request !== evidenceRequest) return;
    const rows = data.sources.map(item => {
      const row = node("section");
      let name = node("span", sourceName(item.source));
      if (!/^[a-z][a-z0-9+.-]*:/i.test(item.source)) {
        const file = new URLSearchParams({ source: item.source });
        if (item.sha256) file.set("sha256", item.sha256);
        name = node("a", sourceName(item.source));
        name.href = "/api/evidence?" + file;
        name.title = item.source;
        const page = /(?:^|;)page:(\d+)/.exec(item.locator || "");
        if (page) name.hash = "page=" + page[1];
        name.target = "_blank";
        name.rel = "noopener";
      }
      row.append(name);
      if (item.locator) row.append(node("small", locatorText(item.locator), "meta"));
      if (item.quote) row.append(node("blockquote", item.quote));
      if (item.translation) row.append(node("p", item.translation));
      if (!/^[a-z][a-z0-9+.-]*:/i.test(item.source)) {
        const inspect = node("button", "Show the passage");
        inspect.onclick = () => showSource(item);
        row.append(inspect);
      }
      return row;
    });
    for (const forecast of data.forecasts) {
      rows.push(sourceButton(forecast.id, forecast.product, "How the " + productName(forecast.product) + " forecast was built"));
    }
    if (data.sources.length && !/^[a-z][a-z0-9+.-]*:/i.test(data.sources[0].source)) showSource(data.sources[0]);
    sources.replaceChildren(...(rows.length ? rows : [node("p", "No source was recorded for this item.")]));
  } catch {
    if (request === evidenceRequest) sources.textContent = "Could not load sources. Close and try again.";
  }
}
function sourceName(source) {
  const scheme = /^([a-z][a-z0-9+.-]*):(.*)$/i.exec(source);
  if (scheme?.[1] === "mail") return "Email";
  if (scheme?.[1] === "human") return "Answer from " + roleName(scheme[2]);
  if (scheme) return source;
  const parts = source.split("/");
  const file = parts.pop();
  const folder = parts.pop();
  return folder ? file + " (" + roleName(folder) + ")" : file;
}
function locatorText(locator) {
  return locator.split(";").map(part => {
    const [key, value] = part.split(":");
    return value ? ({ page: "Page", row: "Row", sheet: "Sheet", cell: "Cell", line: "Line" }[key] || roleName(key)) + " " + value : part;
  }).join(", ");
}
function scheduleRefresh() {
  if (!timer) timer = setTimeout(() => { timer = null; refresh(); }, 150);
}
async function refresh() {
  if (refreshing) { pending = true; return; }
  refreshing = true;
  try {
    const response = await fetch("/api/state");
    if (!response.ok) throw new Error("Read failed");
    snapshot = await response.json();
    renderPause();
    if (!byId("board").hidden) refreshBoard();
    if (!byId("delivery").hidden) loadDelivery();
    if (!byId("procurement").hidden) loadProcurement();
    if (!byId("supply").hidden) loadSupply();
    if (!byId("mail").hidden) loadMail();
    if (!stream) {
      stream = new EventSource("/api/events/stream?since_event=" + snapshot.event_id);
      stream.onmessage = scheduleRefresh;
      stream.onopen = () => { liveStatus(); scheduleRefresh(); };
      stream.onerror = () => offline("Reconnecting");
    }
    if (stream.readyState === EventSource.OPEN) liveStatus();
  } catch {
    offline("Connection lost. Retrying");
    if (!timer) timer = setTimeout(() => { timer = null; refresh(); }, 2000);
  } finally {
    refreshing = false;
    if (pending) { pending = false; scheduleRefresh(); }
  }
}
function navigate() {
  const screens = ["integrations", "environment", "quality", "delivery", "procurement", "supply", "board", "mail"];
  const wanted = location.hash.slice(1);
  const active = screens.includes(wanted) ? wanted : "delivery";
  if (active === "delivery") loadDelivery();
  if (active === "procurement") loadProcurement();
  if (active === "supply") loadSupply();
  if (active === "mail") loadMail();
  if (active === "board") refreshBoard();
  if (active === "quality") loadQuality();
  if (active === "integrations") loadIntegrations();
  if (active === "environment") loadEnvironment();
  byId("provenance").hidden = !provenanceReady || active !== "delivery";
  for (const id of screens) {
    byId(id).hidden = id !== active;
    const link = document.querySelector('nav a[href="#' + id + '"]');
    if (id === active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}
function stamp(iso, zone, withDay) {
  if (!iso) return "";
  const options = { hour: "2-digit", minute: "2-digit", hour12: false };
  if (withDay) Object.assign(options, { weekday: "short", day: "numeric", month: "short" });
  try { return new Intl.DateTimeFormat("en-GB", { ...options, timeZone: zone || undefined }).format(new Date(iso)); }
  catch { return new Intl.DateTimeFormat("en-GB", options).format(new Date(iso)); }
}
let provenanceReady = false;
function renderProvenance(p) {
  const box = byId("provenance");
  const badges = { real: "Real data", fictional: "Fictional sample", unstated: "Data source not stated" };
  const company = p.tenant_name || "this company";
  box.className = "provenance " + p.kind;
  box.replaceChildren(node("strong", badges[p.kind] || badges.unstated, "provenance-badge"));
  const headline = (notice, css) => {
    const line = node("p", null, css);
    const open = notice.kind === "decision" && notice.status === "open";
    const who = notice.for_role ? (open ? "Waiting for the " : "For the ") + roleName(notice.for_role).toLowerCase() + ": " : "";
    line.append(node("strong", who + notice.title + "."));
    for (const mail of notice.mails || []) {
      const link = node("a", "Email from " + String(mail.sender || "the supplier").split("<")[0].trim() + ", " + stamp(mail.at, p.timezone, true));
      link.href = "/api/posts/" + encodeURIComponent(mail.post_id);
      link.target = "_blank";
      link.rel = "noopener";
      line.append(" ", link);
    }
    return line;
  };
  // With several notices the banner is tall: it scrolls with the page instead of staying on top.
  box.classList.toggle("tall", Boolean(p.more && p.more.length));
  if (p.lead) {
    box.append(headline(p.lead, "provenance-lead"));
    for (const notice of p.more || []) box.append(headline(notice, "provenance-lead provenance-more"));
    if (p.lead.detail) {
      const more = node("details", null, "provenance-why");
      more.append(node("summary", "Details"), node("p", p.lead.detail));
      for (const notice of p.more || []) if (notice.detail) more.append(node("p", notice.detail));
      if (p.run) more.append(node("p", p.run));
      box.append(more);
    }
  }
  if (p.kind === "real") {
    const files = p.files ? count.format(p.files) + " files" : "their files";
    box.append(node("span", `From ${company}'s portal files (${files}), unedited.`));
    document.title = "Real data · " + document.title.replace(/^Real data · /, "");
  } else if (p.kind === "fictional") {
    box.append(node("span", `${company} is a made-up company. These numbers are not real.`));
  } else {
    box.append(node("span", `The configuration of ${company} does not say whether these numbers are real.`));
  }
  const run = p.run || (p.started_at ? `Agents started ${stamp(p.started_at, p.timezone, true)} and last acted ${stamp(p.last_activity_at, p.timezone, true)}.` : "");
  if (run && !(p.lead && p.lead.detail)) box.append(node("span", run, "provenance-run"));
  if (p.forecast_at) box.append(node("span", `Forecast from ${stamp(p.forecast_at, p.timezone, true)}.`, "provenance-run"));
  provenanceReady = true;
  box.hidden = byId("delivery").hidden;
}
async function loadProvenance() {
  try {
    const response = await fetch("/api/provenance");
    if (response.ok) renderProvenance(await response.json());
  } catch {
    byId("provenance").hidden = true;
  }
}
window.addEventListener("hashchange", navigate);
navigate();
refresh();
loadProvenance();
