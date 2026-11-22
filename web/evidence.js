"use strict";

let feedRequest, previewRequest;
async function refreshBoard() {
  feedRequest?.abort();
  feedRequest = new AbortController();
  try {
    const response = await fetch("/api/feed?limit=200", {signal: feedRequest.signal});
    if (!response.ok) throw Error();
    const posts = await response.json();
    const topics = new Map();
    // Raw events (clock ticks, file changes, pause and resume) are inputs, not work the agents did.
    for (const post of posts.filter(item => item.kind !== "event")) {
      const topic = topicOf(post);
      if (!topics.has(topic)) topics.set(topic, []);
      topics.get(topic).push(post);
    }
    const cards = Array.from(topics, ([topic, items]) => {
      const card = node("article");
      const latest = items[0];
      card.append(node("small", [roleName(latest.author), when(latest.updated_at)].join(" · "), "meta"),
        node("h3", topic), activityLine(latest));
      if (items.length > 1) {
        const history = node("details");
        history.append(node("summary", (items.length - 1) + " earlier update" + (items.length > 2 ? "s" : "")));
        history.append(...items.slice(1).map(item => {
          const line = activityLine(item);
          line.prepend(node("small", roleName(item.author) + " · " + when(item.updated_at) + " · ", "meta"));
          return line;
        }));
        card.append(history);
      }
      return card;
    });
    byId("feed").replaceChildren(...(cards.length ? cards : [node("p", "The agents have not done anything yet.", "empty")]));
  } catch (error) {
    if (error.name !== "AbortError") byId("feed").textContent = "Could not load Board updates.";
  }
}

const VERBS = {
  claim: "read a value", fact: "confirmed a value", defect: "flagged a data problem", forecast: "updated the forecast",
  decision: "asked for a decision", action: "proposed an action", question: "asked a question", notice: "posted a notice",
  event: "recorded an event",
};
function topicOf(post) {
  if (post.kind === "forecast") return "Launch forecast";
  const parts = (post.subject || post.kind).split(/[/|:]/).map(part => part.trim()).filter(Boolean);
  return parts.slice(0, 2).map(roleName).join(" · ") || roleName(post.kind);
}
function when(stamp) {
  const date = new Date(stamp);
  return Number.isFinite(date.getTime()) ? date.toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" }) : "";
}
function activityLine(post) {
  const line = node("p");
  const body = post.body || {};
  const what = body.title || body.detail || body.text || body.problem;
  line.append(roleName(post.author) + " " + (VERBS[post.kind] || roleName(post.kind).toLowerCase()) + (what ? ": " + what : ""));
  const value = body.value == null ? null
    : [typeof body.value === "object" ? JSON.stringify(body.value) : String(body.value), body.unit].filter(Boolean).join(" ");
  // A Source link that would open an empty dialog is left out; the value then reads as plain text.
  if (post.has_sources) line.append(" ", sourceButton(post.id, null, value ?? "Source"));
  else if (value) line.append(" " + value);
  return line;
}

async function showSource(ref) {
  previewRequest?.abort();
  const controller = new AbortController();
  previewRequest = controller;
  const view = byId("excerpt");
  view.replaceChildren(node("h3", "Source excerpt"), node("small", [ref.source, ref.locator].filter(Boolean).join(" · "), "meta"));
  if (/^(mail|human|sim):/.test(ref.source)) return;
  const params = new URLSearchParams({source:ref.source});
  if (ref.sha256) params.set("sha256", ref.sha256);
  const url = "/api/evidence?" + params;
  if (/\.(png|jpe?g|webp)$/i.test(ref.source)) {
    const img = node("img");
    img.alt = ref.locator || "Source image";
    img.src = url;
    view.append(img);
    return;
  }
  try {
    const response = await fetch("/api/evidence/preview", {method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify(ref), signal:controller.signal});
    if (!response.ok) throw Error();
    const {before, match, after, table} = await response.json();
    if (table) {
      view.append(...excerptTable(table));
      view.querySelector("mark, tr.hit")?.scrollIntoView({block:"nearest", inline:"center"});
      return;
    }
    if (!before && !match && !after) { view.append(node("p", "Open the source file to inspect this reference.")); return; }
    const pre = node("pre");
    pre.append(before);
    if (match) pre.append(node("mark", match));
    pre.append(after);
    view.append(pre);
    if (!match && ref.quote) view.append(node("p", "The quote could not be located in the source text."));
  } catch (error) {
    if (error.name !== "AbortError") view.append(node("p", "Source preview unavailable."));
  }
}
// The rows around the cited place, with its row and cell marked.
function excerptTable(table) {
  const head = el("tr", "", node("th", "Row", "num"), ...table.headers.map((name, i) => node("th", name, i === table.column ? "hit" : "")));
  const rows = table.rows.map(row => el("tr", row.hit ? "hit" : "", node("td", String(row.number), "num"),
    ...row.cells.map((value, i) => row.marked.includes(i) ? el("td", "", node("mark", value || "(empty)")) : node("td", value))));
  const shown = table.rows.length ? "Rows " + table.rows[0].number + " to " + table.rows.at(-1).number : "No rows";
  return [node("small", [table.sheet && "Sheet " + table.sheet, shown].filter(Boolean).join(" · "), "meta"),
    el("div", "table-wrap", el("table", "", el("thead", "", head), el("tbody", "", ...rows)))];
}
