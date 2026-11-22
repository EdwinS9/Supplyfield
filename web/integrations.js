"use strict";

// Integrations: the sources the agents read for this company.
// Loaded before control.js; uses node, byId, el, pill, count at call time.
let integrationsRequest;

function megabytes(bytes) {
  return bytes >= 1e6 ? (bytes / 1e6).toFixed(1) + " MB" : Math.max(1, Math.round(bytes / 1e3)) + " kB";
}
function hourText(hour) {
  return String(hour).padStart(2, "0") + ":00";
}
function every(seconds) {
  if (!seconds) return "on demand";
  return seconds % 60 ? "every " + seconds + " seconds" : "every " + (seconds / 60) + " minute" + (seconds === 60 ? "" : "s");
}
function fact(label, value) {
  return el("div", "fact", node("small", label, "label"), value instanceof Node ? value : node("span", value));
}
function chips(items) {
  return el("div", "tags", ...items.map(text => node("span", text, "chip")));
}
function listenerCard(listener) {
  const mirror = listener.mirror;
  const hours = Array.isArray(listener.active_hours) && listener.active_hours.length === 2
    ? ", " + hourText(listener.active_hours[0]) + " to " + hourText(listener.active_hours[1]) : "";
  const card = el("article", "connector",
    el("div", "connector-head", el("div", "", node("small", "Listener", "label"), node("h3", "Supplier portal")),
      pill(mirror ? "Connected" : "No files yet", mirror ? "good" : "warn")),
    el("div", "facts",
      fact("Adapter", node("span", listener.kind || "not set", "mono")),
      fact("Checks", every(listener.poll_seconds) + hours),
      fact("Files", mirror ? count.format(mirror.files) + " · " + megabytes(mirror.bytes) : "none"),
      fact("Last sync", mirror?.last_synced_at ? dayTime(mirror.last_synced_at) : "never")));
  if (mirror) {
    card.append(el("div", "", node("small", "Formats", "label"),
      chips(mirror.formats.map(item => item.format.toUpperCase() + " " + item.files))));
    const link = node("a", "Data quality", "more");
    link.href = "#quality";
    card.append(link);
  }
  return card;
}
function mailboxCard(mailboxes) {
  const live = mailboxes.outbound === "live";
  const rows = mailboxes.inboxes.map(box => el("li", "", node("span", roleName(box.role)), node("span", box.address, "mono")));
  return el("article", "connector",
    el("div", "connector-head", el("div", "", node("small", "Mail", "label"), node("h3", "Supplier mailboxes")),
      pill(live ? "Sending live" : "Stand-in", live ? "good" : "idle")),
    rows.length ? el("ul", "mailboxes", ...rows) : node("p", "No inbox configured.", "meta"));
}
async function loadIntegrations() {
  integrationsRequest?.abort();
  integrationsRequest = new AbortController();
  try {
    const response = await fetch("/api/integrations", { signal: integrationsRequest.signal });
    if (!response.ok) throw new Error("Read failed");
    const view = await response.json();
    const company = view.company;
    byId("integrations-company").replaceChildren(
      figure("Company", company.name || "Not set", "", [company.site, company.timezone].filter(Boolean).join(" · ")));
    byId("connectors").replaceChildren(listenerCard(view.listener), mailboxCard(view.mailboxes));
  } catch (error) {
    if (error.name !== "AbortError") byId("connectors").replaceChildren(node("p", "Could not load. Try again.", "empty"));
  }
}
