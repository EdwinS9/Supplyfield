"use strict";

// The dashboards: deliveries to clients, procurement from suppliers, and stock with prices and negotiations.
// Loaded before control.js; its helpers (byId, node, count, euros, sourceButton, roleName) are used at call time.
let delivery;
let procurement;
let supply;

const STATUS_LABEL = {
  executed: "Done", approved: "Approved", allowed: "Allowed", requested: "Proposed", needs_approval: "Waiting for approval",
  blocked: "Blocked", rejected: "Rejected", failed: "Failed", held: "Held", on_time: "On time", late: "Late",
};
const STAGE_LABEL = { asked: "Asked", reminded: "Reminded", replied: "Reply received", no_answer: "No answer" };

function el(tag, css, ...children) {
  const element = document.createElement(tag);
  if (css) element.className = css;
  element.append(...children.filter(child => child != null));
  return element;
}
function units(value) {
  return value == null ? "n/a" : count.format(value);
}
function clockTime(stamp) {
  const date = new Date(stamp);
  if (!stamp || Number.isNaN(date.getTime())) return "";
  return date.toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" });
}
// The records give many dates without a time. Such a date is shown as a day, never as midnight UTC.
const BARE_DATE = /^\d{4}-\d{2}-\d{2}$/;
function dayTime(stamp) {
  if (BARE_DATE.test(stamp || "")) {
    const date = new Date(stamp + "T12:00:00");
    // A date in another year names the year, so an old product's deadline does not read as this week.
    const year = date.getFullYear() === new Date().getFullYear() ? {} : { year: "numeric" };
    return date.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short", ...year });
  }
  const date = new Date(stamp);
  if (!stamp || Number.isNaN(date.getTime())) return stamp || "";
  return date.toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}
function pill(text, tone) {
  return node("span", text, "pill " + tone);
}
function figure(label, value, tone, note) {
  return el("div", "figure " + (tone || ""), node("small", label), node("strong", value), note ? node("span", note) : null);
}
function sharePct(value) {
  if (value == null) return "n/a";
  return (value >= 99.95 || value === 0 ? value.toFixed(0) : value.toFixed(1)) + "%";
}
function cell(text, css) {
  return node("td", text, css);
}
function head(...labels) {
  const row = document.createElement("tr");
  for (const label of labels) {
    const th = node("th", label.replace(/^#/, ""));
    if (label.startsWith("#")) th.className = "num";
    th.scope = "col";
    row.append(th);
  }
  return el("thead", "", row);
}
async function readJson(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error("Read failed");
  return response.json();
}

// -- Procurement -----------------------------------------------------------------------------------
const CHANGE_LABEL = {
  unblocked: ["Unblocked", "good"], earlier: ["Earlier", "good"], new: ["New order", "good"],
  corrected: ["Qty corrected", "warn"], stuck: ["Still stuck", "bad"], same: ["Unchanged", "idle"], delivered: ["Delivered", "idle"],
};
const openRows = new Set();

async function loadProcurement() {
  try {
    procurement = await readJson("/api/procurement");
    renderProcurement();
  } catch {
    if (!procurement) byId("procurement-orders").replaceChildren(node("p", "Could not load. Retrying.", "empty"));
  }
}
function changeBar(product) {
  const scale = Math.max(product.late_before || 0, product.late_now || 0, 1);
  const bar = el("div", "change-bar");
  const before = el("span", "before");
  before.style.width = (product.late_before || 0) / scale * 100 + "%";
  const now = el("span", "now");
  now.style.width = (product.late_now || 0) / scale * 100 + "%";
  bar.append(before, now);
  bar.title = units(product.late_before) + " copies late at first, " + units(product.late_now) + " now";
  return bar;
}
function statusOf(product) {
  if (product.status === "on_time") return pill(product.late_before ? "Now on time" : "On time", "good");
  if (product.status === "late") return pill(product.recovered > 0 ? "Fewer late" : "Late at deadline", product.recovered > 0 ? "warn" : "bad");
  return pill("No forecast", "idle");
}
function expected(side) {
  const box = el("div", "expected");
  if (side.at) box.append(node("strong", dayTime(side.at)));
  if (side.text) box.append(node(side.at ? "small" : "span", side.text, side.at ? "" : "blocked"));
  if (!side.at && !side.text) box.append(node("span", "No date on record", "blocked"));
  return box;
}
function stepBlock(step) {
  const tone = ["blocked", "rejected", "failed"].includes(step.status) ? "bad" : step.status === "executed" ? "good" : "warn";
  const block = el("li", "story", el("div", "story-head", node("h4", step.title),
    pill(STATUS_LABEL[step.status] || step.status, tone), step.cost_eur ? pill("Cost " + euros.format(step.cost_eur), "idle") : null));
  const list = document.createElement("ul");
  for (const bullet of step.bullets) {
    const item = document.createElement("li");
    if (bullet.label) item.append(node("strong", bullet.label + ". "));
    item.append(bullet.text);
    if (bullet.at) item.append(node("time", " " + clockTime(bullet.at)));
    list.append(item);
  }
  block.append(list, sourceButton(step.post_id));
  return block;
}
function detailRow(key, steps, columns, emptyText) {
  const row = el("tr", "detail");
  row.hidden = !openRows.has(key);
  const td = document.createElement("td");
  td.colSpan = columns;
  const list = el("ol", "stories");
  list.append(...steps.map(stepBlock));
  td.append(steps.length ? list : node("p", emptyText, "meta"));
  row.append(td);
  return row;
}
function expandable(row, key, detail) {
  const toggle = node("button", "", "toggle");
  toggle.type = "button";
  const set = open => {
    detail.hidden = !open;
    row.classList.toggle("open", open);
    toggle.textContent = open ? "Hide" : "Steps";
    toggle.setAttribute("aria-expanded", String(open));
    if (open) openRows.add(key); else openRows.delete(key);
  };
  const flip = () => set(detail.hidden);
  toggle.onclick = event => { event.stopPropagation(); flip(); };
  row.tabIndex = 0;
  row.ondblclick = flip;
  row.onkeydown = event => { if (event.key === "Enter" && event.target === row) flip(); };
  set(openRows.has(key));
  return toggle;
}
function lateNowTone(product) {
  if (!product.late_now) return "good";
  return product.recovered > 0 ? "warn" : "bad";
}
function productsTable(products) {
  const body = document.createElement("tbody");
  // A product with no demand and no forecast is not part of this launch.
  for (const product of products.filter(p => p.ordered != null || p.late_now != null)) {
    const row = document.createElement("tr");
    row.append(el("td", "order-name", node("strong", product.name), product.premium ? pill("Premium", "accent") : null,
      product.name !== product.id ? node("small", product.id) : null),
      cell(units(product.ordered), "num"), cell(dayTime(product.deadline)),
      cell(units(product.late_before), "num " + (product.late_before ? "bad" : "")),
      cell(units(product.late_now), "num " + lateNowTone(product)),
      el("td", "", changeBar(product)), el("td", "", statusOf(product)));
    body.append(row);
  }
  return products.length
    ? el("table", "data", head("Product", "#Copies ordered", "Deadline", "#Late, first forecast", "#Late, latest forecast", "First and latest", "Status"), body)
    : node("p", "No products yet.", "empty");
}
function renderProcurement() {
  const delivery = procurement;
  byId("procurement-products").replaceChildren(productsTable(delivery.products));

  const orders = document.createElement("tbody");
  for (const order of delivery.deliveries) {
    const key = "order:" + order.id;
    const row = document.createElement("tr");
    const detail = detailRow(key, order.steps, 8, "No steps.");
    const [label, tone] = CHANGE_LABEL[order.change] || [order.change, "idle"];
    const quantity = el("td", "num", order.qty_before != null ? node("s", units(order.qty_before)) : null,
      order.qty_before != null ? " " : null, units(order.qty));
    row.append(el("td", "order-name", node("strong", order.id),
      node("small", order.type === "purchase" ? "Ordered by the agents" : order.type === "po_line" ? "Purchase order line"
        : order.po_numbers.length ? "PO " + order.po_numbers.join(", ") : "Shipment")),
      cell(order.supplier), el("td", "", node("span", order.part || "n/a"), node("small", order.used_by.join(", "), "sub")),
      quantity, el("td", "", expected(order.without)), el("td", "", expected(order.with_agents)), el("td", "", pill(label, tone)));
    row.append(el("td", "num", expandable(row, key, detail)));
    orders.append(row, detail);
  }
  byId("procurement-orders").replaceChildren(delivery.deliveries.length
    ? el("table", "data rows", head("Order", "Supplier", "Part", "#Quantity", "Expected without agents", "Expected with agents", "Result", "#"), orders)
    : node("p", "No orders yet.", "empty"));

  const others = document.createElement("tbody");
  for (const step of delivery.other_actions) {
    const key = "step:" + step.post_id;
    const row = document.createElement("tr");
    const detail = detailRow(key, [step], 4, "");
    const tone = ["blocked", "rejected", "failed"].includes(step.status) ? "bad" : step.status === "executed" ? "good" : "warn";
    row.append(cell(clockTime(step.at)), el("td", "order-name", node("strong", step.title)),
      el("td", "", pill(STATUS_LABEL[step.status] || step.status, tone)));
    row.append(el("td", "num", expandable(row, key, detail)));
    others.append(row, detail);
  }
  byId("other-actions").replaceChildren(delivery.other_actions.length
    ? el("table", "data rows", head("When", "Action", "Status", "#"), others)
    : node("p", "None.", "empty"));
}

// -- Deliveries to clients ---------------------------------------------------------------------------
const CLIENT_LABEL = {
  saved: ["Now on time", "good"], better: ["Fewer late", "warn"], worse: ["More late", "bad"],
  late: ["Late at deadline", "bad"], on_time: ["On time", "good"], unknown: ["No forecast", "idle"],
};
async function loadDelivery() {
  try {
    delivery = await readJson("/api/delivery");
    renderDelivery();
  } catch {
    if (!delivery) byId("client-orders").replaceChildren(node("p", "Could not load. Retrying.", "empty"));
  }
}
function clientExpected(side, order, latest) {
  const box = el("div", "expected");
  if (!side) {
    box.append(node("span", "No forecast yet", "blocked"));
  } else if (!side.late) {
    box.append(node("strong", dayTime(order.deadline)));
    box.append(node("small", side.ready_at === "packed" ? "Already packed" : side.ready_at ? "Packed by " + clockTime(side.ready_at) : "All copies on the promised date", "fine"));
  } else {
    box.append(node("strong", units(side.on_time) + " on " + dayTime(order.deadline)));
    box.append(node("span", units(side.late) + " copies miss the deadline", "blocked"));
    if (latest) box.append(node("small", recoveryNote(order)));
  }
  return box;
}
// The late copies have no date in the forecast: their recovery runs through orders and approvals.
function recoveryNote(order) {
  const waiting = order.steps.filter(step => step.status === "needs_approval").length;
  return waiting
    ? "Recovery after the deadline, " + waiting + (waiting === 1 ? " step waits" : " steps wait") + " for approval"
    : "Recovery after the deadline, see the notices above";
}
function renderDelivery() {
  // Only deadlines that lie ahead count: an older product's past date is not this launch.
  const today = String(delivery.now || "").slice(0, 10);
  const days = delivery.products.map(p => p.deadline).filter(d => BARE_DATE.test(d || "") && d >= today).sort();
  const deadlines = delivery.products.map(p => p.deadline).filter(d => d && !BARE_DATE.test(d)).map(d => Date.parse(d))
    .filter(t => Number.isFinite(t) && t >= Date.parse(delivery.now));
  const left = deadlines.length ? Math.min(...deadlines) - Date.parse(delivery.now) : null;
  byId("delivery-clock").textContent = left != null ? Math.floor(left / 3600000) + "h " + Math.floor(left % 3600000 / 60000) + "m"
    : days.length ? (days[0] === today ? "Today, " : "") + dayTime(days[0])
    : delivery.products.some(p => p.deadline) ? "Deadline reached" : "No deadline on record";
  byId("delivery-products").replaceChildren(productsTable(delivery.products));

  const body = document.createElement("tbody");
  for (const order of delivery.orders) {
    const key = "client:" + order.id;
    const row = document.createElement("tr");
    const detail = detailRow(key, order.steps, 7, "No steps.");
    const [label, tone] = CLIENT_LABEL[order.change] || [order.change, "idle"];
    row.append(el("td", "order-name", node("strong", order.label), order.premium ? pill("Premium", "accent") : null,
      node("small", order.product)),
      cell(units(order.qty), "num"), cell(dayTime(order.deadline)),
      el("td", "", clientExpected(order.without, order, false)), el("td", "", clientExpected(order.with_agents, order, true)),
      el("td", "", pill(label, tone)));
    row.append(el("td", "num", expandable(row, key, detail)));
    body.append(row, detail);
  }
  byId("client-orders").replaceChildren(delivery.orders.length
    ? el("table", "data rows", head("Client order", "#Copies", "Promised for", "First forecast", "Latest forecast", "Result", "#"), body)
    : node("p", "No client orders yet.", "empty"));
}

// -- Stock and prices ------------------------------------------------------------------------------
async function loadSupply() {
  try {
    supply = await readJson("/api/supply");
    renderSupply();
  } catch {
    if (!supply) byId("stock").replaceChildren(node("p", "Could not load. Retrying.", "empty"));
  }
}
function coverBar(row) {
  const share = row.needed ? Math.min((row.usable + row.incoming) / row.needed, 1) : 1;
  const bar = el("div", "cover " + (share >= 1 ? "good" : share >= .8 ? "warn" : "bad"));
  const fill = document.createElement("span");
  fill.style.width = share * 100 + "%";
  bar.append(fill);
  bar.title = row.needed ? units(row.usable) + " usable and " + units(row.incoming) + " on the way, of " + units(row.needed) + " still needed" : "No open demand";
  return el("div", "cover-cell", bar, node("small", row.needed ? sharePct(share * 100) : ""));
}
function talkCard(talk) {
  const tone = talk.stage === "replied" ? "good" : talk.stage === "no_answer" ? "bad" : "warn";
  const card = el("article", "talk", el("div", "tags", pill(STAGE_LABEL[talk.stage] || talk.stage, tone),
    talk.decision ? pill(talk.decision.status === "waiting" ? "Waiting for " + roleName(talk.decision.role) : "Decided by " + roleName(talk.decision.role),
      talk.decision.status === "waiting" ? "accent" : "idle") : null),
    node("h3", talk.supplier));
  for (const mistake of talk.mistakes) card.append(el("p", "", node("small", "Their mistake", "label"), node("span", mistake.detail || mistake.what)));
  if (talk.terms.length) {
    const terms = el("ul", "terms");
    for (const term of talk.terms) {
      terms.append(node("li", term.entity === "observation" ? "“" + (term.quote || term.value) + "”"
        : term.field.replaceAll("_", " ") + " " + term.value + (term.unit ? " " + term.unit : "") + (term.quote ? " (“" + term.quote + "”)" : "")));
    }
    card.append(el("div", "", node("small", "What they offer", "label"), terms));
  } else if (talk.stage === "replied") {
    card.append(node("p", "The reply names no price reduction or credit.", "meta"));
  }
  const trail = el("ol", "trail");
  for (const step of talk.steps) trail.append(el("li", "", node("time", clockTime(step.at)),
    node("span", step.text + (step.ask > 1 ? " (email " + step.ask + ")" : "") + ".")));
  card.append(trail);
  return card;
}
function renderSupply() {
  const t = supply.totals;
  const short = supply.stock.filter(row => row.runs_out_at).length;
  byId("supply-figures").replaceChildren(
    figure("Parts running out", String(short), short ? "warn" : "good", "of " + supply.stock.length),
    figure("Units held", units(supply.stock.reduce((sum, row) => sum + row.held, 0)), "", ""),
    figure("Committed spend", euros.format(t.spend_eur), "", t.committed_orders + (t.committed_orders === 1 ? " order" : " orders")),
    figure("Negotiations", String(supply.negotiations.length), "",
      t.negotiations_replied + " replied, " + t.negotiations_open + " waiting, " + t.negotiations_unanswered + " unanswered"));

  const body = document.createElement("tbody");
  for (const row of supply.stock) {
    const tr = document.createElement("tr");
    tr.append(el("td", "order-name", node("strong", row.name), row.name !== row.part_id ? node("small", row.part_id) : null),
      cell(row.used_by.join(", ")), cell(units(row.usable), "num"), cell(units(row.incoming), "num"), cell(row.held ? units(row.held) : "0", "num " + (row.held ? "warn" : "")),
      cell(units(row.needed), "num"), el("td", "", coverBar(row)),
      el("td", "", row.runs_out_at ? pill("Runs out " + clockTime(row.runs_out_at), "warn") : pill("Lasts", "good")));
    body.append(tr);
  }
  byId("stock").replaceChildren(supply.stock.length
    ? el("table", "data", head("Part", "Used by", "#Usable now", "#On the way", "#Held", "#Still needed", "Cover", "Until the deadline"), body)
    : node("p", "No stock yet.", "empty"));

  byId("negotiations").replaceChildren(...(supply.negotiations.length ? supply.negotiations.map(talkCard)
    : [node("p", "No negotiations yet.", "empty")]));

  const bought = document.createElement("tbody");
  for (const p of supply.purchases) {
    const tr = document.createElement("tr");
    const price = p.unit_price == null ? "n/a" : p.unit_price.toFixed(2) + " " + (p.currency || "");
    const listed = p.listed_price == null ? "n/a" : p.listed_price.toFixed(2);
    tr.append(cell(clockTime(p.at)), cell(p.supplier), cell(p.part), cell(units(p.qty), "num"), cell(price, "num"),
      cell(listed, "num"), cell(p.cost_eur == null ? "n/a" : euros.format(p.cost_eur), "num"),
      el("td", "", pill(STATUS_LABEL[p.status] || p.status, p.status === "executed" ? "good" : "warn")),
      el("td", "num", sourceButton(p.post_id)));
    bought.append(tr);
  }
  byId("purchases").replaceChildren(supply.purchases.length
    ? el("table", "data", head("When", "Supplier", "Part", "#Quantity", "#Unit price", "#Listed price", "#Total", "Status", "#"), bought)
    : node("p", "No purchases yet.", "empty"));
}
