"use strict";

// Mail traffic: structured admin view of emails sent by and received by the agents.
// Loaded before control.js; uses byId, node, el, pill, figure, cell, head, count, dayTime, roleName, sourceButton, readJson.
let mailData;
let mailRequest;
let mailMode = "threads";
const openMailThreads = new Set();
const openMailRows = new Set();
const mailBodyCache = new Map();
const mailTranslationCache = new Map();
const mailShowOriginal = new Set();

const ROUTE_META = {
  live: ["Live", "good"],
  "stand-in": ["Stand-in", "warn"],
  internal: ["Internal", "idle"],
  inbound: ["Inbound", "good"],
  pending: ["Not sent yet", "warn"],
  not_sent: ["Not sent", "bad"],
};

const MAIL_STATUS = {
  executed: ["Sent", "good"],
  received: ["Received", "good"],
  requested: ["Proposed", "warn"],
  allowed: ["Queued", "warn"],
  approved: ["Approved", "warn"],
  needs_approval: ["Needs approval", "warn"],
  blocked: ["Blocked", "bad"],
  rejected: ["Declined", "bad"],
  failed: ["Failed", "bad"],
};

function mailCacheKey(msg) {
  return (msg.inbox || "") + "::" + (msg.message_id || msg.id);
}

function directionPill(direction) {
  return direction === "inbound" ? pill("Received", "good") : pill("Sent", "accent");
}

function routePill(route) {
  const [label, tone] = ROUTE_META[route] || [route || "Unknown", "idle"];
  return pill(label, tone);
}

function mailStatusPill(status) {
  const [label, tone] = MAIL_STATUS[status] || [status || "Unknown", "idle"];
  return pill(label, tone);
}

function matchesMailFilters(msg) {
  const dir = byId("mail-direction").value;
  const inbox = byId("mail-inbox").value;
  const status = byId("mail-status").value;
  const q = byId("mail-search").value.trim().toLowerCase();
  if (dir !== "all" && msg.direction !== dir) return false;
  if (inbox !== "all" && msg.inbox !== inbox) return false;
  if (status === "sent" && !["executed", "received"].includes(msg.status)) return false;
  if (status === "pending" && !["requested", "allowed", "needs_approval", "approved"].includes(msg.status)) return false;
  if (status === "issues" && !["blocked", "rejected", "failed"].includes(msg.status)) return false;
  if (q) {
    const hay = [msg.subject, msg.counterparty, msg.from, msg.to, msg.intended_to, msg.preview, msg.inbox, msg.agent]
      .filter(Boolean).join(" ").toLowerCase();
    if (!hay.includes(q)) return false;
  }
  return true;
}

async function fetchFullBody(msg, bodyBox, metaBox) {
  const key = mailCacheKey(msg);
  if (mailBodyCache.has(key)) {
    const cached = mailBodyCache.get(key);
    msg.body = cached.body;
    msg.body_en = msg.body_en || cached.body_en;
    msg.intended_to = msg.intended_to || cached.intended_to;
    if (cached.attachments?.length && !msg.attachments?.length) msg.attachments = cached.attachments;
    msg.has_body = true;
    renderMessageBody(msg, bodyBox, metaBox);
    return;
  }
  if (!msg.inbox || !msg.message_id) {
    bodyBox.replaceChildren(node("p", "No message body recorded.", "meta"));
    return;
  }
  bodyBox.replaceChildren(node("p", "Loading full email…", "meta"));
  try {
    const params = new URLSearchParams({ inbox: msg.inbox, message_id: msg.message_id });
    const detail = await readJson("/api/mail/message?" + params);
    mailBodyCache.set(key, detail);
    msg.body = detail.body;
    msg.body_en = msg.body_en || detail.body_en;
    msg.intended_to = msg.intended_to || detail.intended_to;
    if (detail.attachments?.length) msg.attachments = detail.attachments;
    msg.has_body = true;
    renderMessageBody(msg, bodyBox, metaBox);
  } catch {
    bodyBox.replaceChildren(node("p", "Could not load full email body.", "meta"));
  }
}

function getTranslation(msg) {
  const key = mailCacheKey(msg);
  if (mailTranslationCache.has(key)) return mailTranslationCache.get(key);
  if (msg.body_en) {
    return {
      subject: msg.subject || "",
      body: msg.body_en,
      source_language: msg.language ? msg.language.toUpperCase() : "",
      target_language: "English",
      already_in_target: false,
      from_board: true,
    };
  }
  return null;
}

function parseStreamedTranslation(raw, fallbackSubject) {
  let source = "";
  let subject = fallbackSubject || "";
  const lines = raw.split(/\r?\n/);
  let idx = 0;
  while (idx < lines.length && idx < 4) {
    const line = lines[idx].trim();
    const low = line.toLowerCase();
    if (low.startsWith("source:")) {
      source = line.slice(7).trim();
      idx++;
    } else if (low.startsWith("subject:")) {
      subject = line.slice(8).trim() || subject;
      idx++;
    } else if (!line && idx > 0) {
      idx++;
      break;
    } else {
      break;
    }
  }
  return { source, subject, body: lines.slice(idx).join("\n").replace(/^\s+/, "") };
}

async function translateMessage(msg, bodyBox, metaBox, button) {
  const key = mailCacheKey(msg);
  button.disabled = true;
  button.textContent = "Translating…";
  const prevErr = bodyBox.querySelector(".mail-translate-err");
  if (prevErr) prevErr.remove();

  const controller = new AbortController();
  const statusEl = node("span", "Translating to English…", "meta");
  const cancelBtn = node("button", "Cancel", "mail-lang-toggle");
  cancelBtn.type = "button";
  cancelBtn.onclick = () => {
    controller.abort();
    renderMessageBody(msg, bodyBox, metaBox);
  };
  const liveSubject = el("p", "mail-translated-subject", node("small", "Translated subject ", "label"), node("strong", ""));
  liveSubject.hidden = true;
  const liveBody = node("pre", "Translating…", "mail-body translated");

  try {
    const response = await fetch("/api/mail/translate/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: controller.signal,
      body: JSON.stringify({
        subject: msg.subject || "",
        body: msg.body || "",
        inbox: msg.inbox || null,
        message_id: msg.message_id || null,
        target_language: "English",
      }),
    });
    if (!response.ok || !response.body) {
      const err = await response.json().catch(() => ({}));
      throw new Error(err.detail || "Translation failed");
    }

    bodyBox.replaceChildren(
      el("div", "mail-translation-banner",
        el("div", "mail-translation-info", pill("Translation", "accent"), statusEl),
        cancelBtn),
      liveSubject,
      liveBody);

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    let raw = "";
    let finalPayload = null;

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop() || "";
      for (const line of lines) {
        if (!line.trim()) continue;
        const event = JSON.parse(line);
        if (event.type === "chunk") {
          raw += event.text;
          const partial = parseStreamedTranslation(raw, msg.subject);
          if (partial.source) statusEl.textContent = "Translating from " + partial.source + " to English…";
          if (partial.subject && partial.subject !== (msg.subject || "")) {
            liveSubject.hidden = false;
            liveSubject.querySelector("strong").textContent = partial.subject;
          }
          if (partial.body) liveBody.textContent = partial.body;
        } else if (event.type === "done") {
          finalPayload = event;
        } else if (event.type === "error") {
          throw new Error(event.detail || "Translation failed");
        }
      }
    }
    if (!finalPayload) throw new Error("Translation ended early");
    mailTranslationCache.set(key, finalPayload);
    mailShowOriginal.delete(key);
    renderMessageBody(msg, bodyBox, metaBox);
  } catch (error) {
    if (error?.name === "AbortError") return;
    renderMessageBody(msg, bodyBox, metaBox);
    bodyBox.append(node("p", error?.message || "Could not translate this email.", "mail-error mail-translate-err"));
  }
}

function renderMessageBody(msg, bodyBox, metaBox) {
  const key = mailCacheKey(msg);
  const translation = getTranslation(msg);
  const showingTranslation = Boolean(translation) && !mailShowOriginal.has(key);
  const nodes = [];

  if (showingTranslation) {
    const knownSource = translation.source_language && translation.source_language.toLowerCase() !== "unknown"
      ? translation.source_language : "";
    const note = translation.already_in_target
      ? "Already in " + (translation.target_language || "English")
      : (knownSource
        ? "Translated from " + knownSource + " to " + (translation.target_language || "English")
        : "Translated to " + (translation.target_language || "English"));
    const toggle = node("button", "Show original", "mail-lang-toggle");
    toggle.type = "button";
    toggle.onclick = () => {
      mailShowOriginal.add(key);
      renderMessageBody(msg, bodyBox, metaBox);
    };
    nodes.push(el("div", "mail-translation-banner",
      el("div", "mail-translation-info",
        pill("Translation", "accent"),
        node("span", note, "meta")),
      toggle));
    if (translation.subject && translation.subject !== (msg.subject || "")) {
      nodes.push(el("p", "mail-translated-subject",
        node("small", "Translated subject ", "label"),
        node("strong", translation.subject)));
    }
    nodes.push(node("pre", translation.body || "(empty message)", "mail-body translated"));
  } else {
    const text = msg.body ?? msg.preview ?? "";
    const lang = translation?.source_language || (msg.language ? msg.language.toUpperCase() : "");
    const label = lang ? "Original (" + lang + ")" : "Original message";
    const actionBtn = node("button", translation ? "Show translation" : "Translate to English", "mail-lang-toggle");
    actionBtn.type = "button";
    actionBtn.onclick = () => {
      if (translation) {
        mailShowOriginal.delete(key);
        renderMessageBody(msg, bodyBox, metaBox);
      } else {
        translateMessage(msg, bodyBox, metaBox, actionBtn);
      }
    };
    nodes.push(el("div", "mail-body-bar",
      node("small", label, "label"),
      actionBtn));
    nodes.push(node("pre", text || "(empty message)", "mail-body"));
  }

  bodyBox.replaceChildren(...nodes);
  if (metaBox && msg.intended_to && !metaBox.querySelector(".mail-intended")) {
    metaBox.append(el("div", "fact mail-intended", node("small", "Intended recipient", "label"), node("span", msg.intended_to, "mono")));
  }
}

function renderMessageCard(msg) {
  const card = el("div", "mail-msg " + msg.direction);
  const badges = [directionPill(msg.direction)];
  if (!(msg.direction === "inbound" && msg.status === "received")) badges.push(mailStatusPill(msg.status));
  if (msg.direction === "outbound") badges.push(routePill(msg.route));
  if (msg.agent) badges.push(node("span", roleName(msg.agent), "chip"));
  if (msg.action_kind && msg.action_kind !== "send_email") badges.push(node("span", roleName(msg.action_kind), "chip"));
  card.append(el("div", "mail-msg-head",
    el("div", "mail-msg-badges", ...badges),
    node("time", dayTime(msg.timestamp))));

  const meta = el("div", "mail-meta-grid",
    el("div", "fact", node("small", "From", "label"), node("span", msg.from || "Unknown", "mono")),
    el("div", "fact", node("small", "To", "label"), node("span", msg.to || "Unknown", "mono")),
    msg.intended_to ? el("div", "fact mail-intended", node("small", "Intended recipient", "label"), node("span", msg.intended_to, "mono")) : null,
    el("div", "fact", node("small", "Subject", "label"), node("span", msg.subject || "(no subject)")));
  card.append(meta);

  const bodyBox = el("div", "mail-body-wrap");
  card.append(bodyBox);
  if (msg.has_body || (!msg.message_id && msg.preview)) {
    renderMessageBody(msg, bodyBox, meta);
  } else {
    fetchFullBody(msg, bodyBox, meta);
  }

  if (msg.verdict?.reason) {
    card.append(el("p", "meta mail-note", node("strong", "Gate ruling: "), msg.verdict.reason));
  }
  if (msg.error) {
    card.append(el("p", "mail-error", node("strong", "Error: "), msg.error));
  }
  if (msg.attachments?.length) {
    card.append(el("div", "mail-extra", node("small", "Attachments", "label"),
      el("div", "tags", ...msg.attachments.map(name => node("span", name, "chip")))));
  }
  if (msg.claims?.length) {
    const list = el("ul", "mail-claims");
    for (const claim of msg.claims) {
      const value = typeof claim.value === "object" ? JSON.stringify(claim.value) : String(claim.value);
      list.append(el("li", "",
        node("span", [claim.entity, claim.key, claim.field].filter(Boolean).join(" / "), "mono"),
        " = ",
        node("strong", value),
        claim.quote ? node("small", " \"" + claim.quote + "\"", "meta") : null));
    }
    card.append(el("div", "mail-extra", node("small", "Extracted claims (" + msg.claims.length + ")", "label"), list));
  }
  if (msg.post_id) {
    card.append(sourceButton(msg.post_id));
  }
  return card;
}

function renderThreadsView(threads) {
  if (!threads.length) return node("p", "No email threads match the current filters.", "empty");
  const list = el("div", "mail-threads");
  for (const thread of threads) {
    const filtered = thread.messages.filter(matchesMailFilters);
    if (!filtered.length) continue;
    const isOpen = openMailThreads.has(thread.id);
    const panel = el("article", "mail-thread" + (thread.issue_count ? " has-issue" : thread.pending_count ? " has-pending" : ""));
    const toggle = node("button", isOpen ? "Hide" : "Open", "toggle");
    toggle.type = "button";
    toggle.setAttribute("aria-expanded", String(isOpen));

    const summaryPills = [
      thread.outbound_count ? pill(thread.outbound_count + " sent", "accent") : null,
      thread.inbound_count ? pill(thread.inbound_count + (thread.inbound_count === 1 ? " reply" : " replies"), "good") : null,
      thread.pending_count ? pill(thread.pending_count + " pending", "warn") : null,
      thread.issue_count ? pill(thread.issue_count + " blocked/failed", "bad") : null,
      thread.latest_direction === "outbound" ? routePill(thread.latest_route) : null,
    ];
    const header = el("div", "mail-thread-head",
      el("div", "mail-thread-title",
        el("div", "mail-thread-eyebrow",
          node("span", thread.counterparty, "mail-counterparty"),
          thread.role ? node("span", roleName(thread.role) + " · " + thread.inbox, "chip") : (thread.inbox ? node("span", thread.inbox, "chip") : null)),
        node("h3", thread.subject || "(no subject)"),
        thread.latest_preview ? node("p", thread.latest_preview, "meta mail-preview") : null),
      el("div", "mail-thread-right",
        el("div", "mail-msg-badges", ...summaryPills),
        el("div", "mail-thread-actions", node("time", dayTime(thread.latest_at)), toggle)));

    const body = el("div", "mail-thread-body");
    body.hidden = !isOpen;
    if (isOpen) body.append(...filtered.map(renderMessageCard));

    const flip = () => {
      const nowOpen = body.hidden;
      body.hidden = !nowOpen;
      toggle.textContent = nowOpen ? "Hide" : "Open";
      toggle.setAttribute("aria-expanded", String(nowOpen));
      if (nowOpen) {
        openMailThreads.add(thread.id);
        if (!body.childElementCount) body.append(...filtered.map(renderMessageCard));
      } else {
        openMailThreads.delete(thread.id);
      }
    };
    toggle.onclick = event => { event.stopPropagation(); flip(); };
    header.onclick = flip;

    panel.append(header, body);
    list.append(panel);
  }
  return list.childElementCount ? list : node("p", "No email threads match the current filters.", "empty");
}

function renderMessagesTable(messages) {
  const filtered = messages.filter(matchesMailFilters);
  if (!filtered.length) return node("p", "No emails match the current filters.", "empty");
  const tbody = document.createElement("tbody");
  for (const msg of filtered) {
    const key = msg.id;
    const isOpen = openMailRows.has(key);
    const detail = el("tr", "detail");
    detail.hidden = !isOpen;
    const td = document.createElement("td");
    td.colSpan = 6;
    if (isOpen) td.append(renderMessageCard(msg));
    detail.append(td);

    const toggle = node("button", isOpen ? "Hide" : "Open", "toggle");
    toggle.type = "button";
    toggle.setAttribute("aria-expanded", String(isOpen));

    const row = el("tr", isOpen ? "open" : "",
      el("td", "", node("time", dayTime(msg.timestamp))),
      el("td", "", directionPill(msg.direction)),
      el("td", "order-name",
        node("strong", msg.subject || "(no subject)"),
        node("small", msg.counterparty + (msg.intended_to ? " (via stand-in)" : ""))),
      el("td", "", node("span", msg.role ? roleName(msg.role) : (msg.inbox || ""), "mono")),
      el("td", "", el("div", "mail-msg-badges",
        mailStatusPill(msg.status),
        msg.direction === "outbound" ? routePill(msg.route) : null,
        msg.claims?.length ? node("span", msg.claims.length + " claims", "chip") : null)),
      el("td", "num", toggle));

    const flip = () => {
      const nowOpen = detail.hidden;
      detail.hidden = !nowOpen;
      row.classList.toggle("open", nowOpen);
      toggle.textContent = nowOpen ? "Hide" : "Open";
      toggle.setAttribute("aria-expanded", String(nowOpen));
      if (nowOpen) {
        openMailRows.add(key);
        if (!td.childElementCount) td.append(renderMessageCard(msg));
      } else {
        openMailRows.delete(key);
      }
    };
    toggle.onclick = event => { event.stopPropagation(); flip(); };
    row.tabIndex = 0;
    row.ondblclick = flip;
    row.onkeydown = event => { if (event.key === "Enter" && event.target === row) flip(); };
    tbody.append(row, detail);
  }
  return el("div", "table-wrap",
    el("table", "data rows mail-table",
      head("Time", "Direction", "Subject & counterparty", "Inbox", "Status", ""),
      tbody));
}

function populateInboxFilter(inboxes) {
  const select = byId("mail-inbox");
  const current = select.value;
  const options = [new Option("All inboxes", "all")];
  for (const box of inboxes || []) {
    options.push(new Option(roleName(box.role) + " (" + box.address + ")", box.address));
  }
  select.replaceChildren(...options);
  if ([...select.options].some(o => o.value === current)) select.value = current;
}

function renderMail() {
  if (!mailData) return;
  const s = mailData.summary;
  const modeText = mailData.outbound_mode === "live" ? "Live" : "Stand-in";
  const sourceText = mailData.provider_connected ? "Board + AgentMail live" : "Board records";
  byId("mail-meta").textContent = modeText + " · " + sourceText;

  const issues = s.pending + s.issues;
  byId("mail-figures").replaceChildren(
    figure("Emails", count.format(s.total), "", count.format(s.threads) + (s.threads === 1 ? " thread" : " threads")),
    figure("Sent", count.format(s.outbound), "", count.format(s.sent) + " delivered"),
    figure("Received", count.format(s.inbound), s.inbound ? "good" : ""),
    figure("Pending & issues", count.format(issues), s.issues ? "bad" : s.pending ? "warn" : "good",
      count.format(s.pending) + " waiting · " + count.format(s.issues) + " blocked or failed"));

  byId("mail-mode-threads").className = mailMode === "threads" ? "primary" : "";
  byId("mail-mode-list").className = mailMode === "list" ? "primary" : "";

  const parts = [];
  if (mailData.provider_error) {
    parts.push(el("p", "meta mail-provider-warn",
      "AgentMail unreachable (" + mailData.provider_error + "). Showing Board records."));
  }
  parts.push(mailMode === "threads" ? renderThreadsView(mailData.threads) : renderMessagesTable(mailData.messages));
  byId("mail-content").replaceChildren(...parts);
}

async function loadMail(refresh = false) {
  mailRequest?.abort();
  mailRequest = new AbortController();
  const refreshBtn = byId("mail-refresh");
  if (refreshBtn) refreshBtn.disabled = true;
  try {
    const response = await fetch("/api/mail" + (refresh ? "?refresh=1" : ""), { signal: mailRequest.signal });
    if (!response.ok) throw new Error("Read failed");
    mailData = await response.json();
    populateInboxFilter(mailData.inboxes);
    renderMail();
  } catch (error) {
    if (error.name !== "AbortError" && !mailData) {
      byId("mail-content").replaceChildren(node("p", "Could not load. Try again.", "empty"));
    }
  } finally {
    if (refreshBtn) refreshBtn.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  byId("mail-mode-threads")?.addEventListener("click", () => { mailMode = "threads"; renderMail(); });
  byId("mail-mode-list")?.addEventListener("click", () => { mailMode = "list"; renderMail(); });
  for (const id of ["mail-direction", "mail-inbox", "mail-status"]) {
    byId(id)?.addEventListener("change", renderMail);
  }
  byId("mail-search")?.addEventListener("input", renderMail);
  byId("mail-refresh")?.addEventListener("click", () => loadMail(true));
});
