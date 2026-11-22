"use strict";

// Data quality: every finding of the quality report over the customer's files, grouped by kind.
// Every finding, value and file name opens the file at the row and cell it is about. Loaded before control.js; uses node, byId,
// el, pill and showSource at call time.
const SEVERITY = { error: ["Error", "bad"], warn: ["Warning", "warn"], info: ["Note", "idle"] };
let qualityRequest;

function sentence(text) {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : "";
}
function fileName(path) {
  return path.split("/").pop();
}
function folderName(path) {
  return path.split("/").slice(0, -1).join("/");
}
function locatorLabel(locator) {
  return locator ? locator.split(";").map(part => sentence(part.replace(":", " "))).join(", ") : "";
}
function openFile(source, locator, title, column, find) {
  const dialog = byId("evidence");
  const file = new URLSearchParams({ source });
  const link = node("a", fileName(source));
  link.href = "/api/evidence?" + file;
  link.target = "_blank";
  link.rel = "noopener";
  const row = node("section");
  row.append(link, node("small", [folderName(source), locatorLabel(locator)].filter(Boolean).join(" · "), "meta"));
  if (title) row.append(node("p", title));
  byId("sources").replaceChildren(row);
  if (!dialog.open) dialog.showModal();
  showSource({ source, locator, column: column || "", find: find || "" });
}
function fileButton(source, locator, label, title, column, find) {
  const button = node("button", label, "source-link");
  button.type = "button";
  button.title = [source, locatorLabel(locator)].filter(Boolean).join(" · ");
  button.onclick = () => openFile(source, locator, title, column, find);
  return button;
}
// The exact place of a finding: its first example, else its file, sheet and column.
function placeOf(finding) {
  const first = finding.examples.find(example => example.source);
  if (first) return first;
  return finding.source ? { source: finding.source, locator: finding.locator, column: finding.check === "format" ? null : finding.column } : null;
}
function openPlace(place, title) {
  openFile(place.source, place.locator, title, place.column, place.find);
}
// A value in a table that opens the cell it was read from.
function valueButton(text, css, source, locator, title, column, find) {
  const button = fileButton(source, locator, text, title, column, find);
  button.className = css + " ev-open";
  return button;
}
function fileBlock(finding) {
  const block = el("div", "issue-file");
  if (finding.source) {
    block.append(fileButton(finding.source, finding.locator, fileName(finding.source), finding.title,
      finding.check === "format" || finding.columns?.length > 1 ? null : finding.column),
      node("small", folderName(finding.source)));
  } else {
    block.append(node("strong", sentence(finding.label.replace(/^\((.*)\)$/, "$1")), "label"));
  }
  const place = [finding.sheet && "Sheet " + finding.sheet, finding.column && finding.check !== "format" && (finding.columns?.length > 1 ? "Columns " : "Column ") + finding.column];
  if (place.some(Boolean)) block.append(node("small", place.filter(Boolean).join(" · ")));
  return block;
}
function rowLink(example, finding, label) {
  return example.source ? fileButton(example.source, example.locator, label || locatorLabel(example.locator) || "Open", finding.title,
    example.column, example.find) : null;
}
function number(text) {
  const value = Number(text);
  return Number.isFinite(value) ? value.toLocaleString("en-GB") : text;
}
function evidenceTable(labels, rows) {
  return el("div", "table-wrap", el("table", "evidence", el("thead", "", el("tr", "", ...labels.map(label => {
    const th = el("th", "", label);
    th.scope = "col";
    return th;
  }))), el("tbody", "", ...rows)));
}
function qualityCell(...children) {
  return el("td", "", ...children);
}
// A correction: the record, the field, old value struck through, new value, and what the master file still says.
function changeTable(finding, examples) {
  return evidenceTable(["Record", "Field", "Correction", "Master file says", ""], examples.map(({ facts, source, locator, column, find }) => {
    const master = fileButton(facts.master, "", fileName(facts.master), finding.title, facts.field, facts.id);
    return el("tr", "", qualityCell(node("span", facts.id, "ev-id")), qualityCell(node("span", facts.field, "ev-field")),
      qualityCell(el("span", "ev-change", node("s", facts.old), node("span", "→", "arrow"), node("strong", facts.new))),
      qualityCell(valueButton(facts.have, "ev-value " + (facts.have === facts.new ? "" : "warn"), facts.master, "", finding.title,
        facts.field, facts.id), el("small", "", "in ", master)),
      qualityCell(rowLink({ source, locator, column, find }, finding)));
  }));
}
// Two files about the same record: one column per file, the two values side by side.
function compareTable(finding, examples) {
  const [first] = examples;
  const here = first.facts.here, there = first.facts.there.replace(/ \[[^\]]+\]$/, "");
  const sheet = (first.facts.there.match(/\[([^\]]+)\]$/) || [])[1];
  const header = path => fileButton(path, "", fileName(path), finding.title);
  return evidenceTable(["Record", header(here), header(there)], examples.map(({ facts }) => el("tr", "",
    qualityCell(node("span", facts.id, "ev-id"), facts.rows ? node("small", facts.rows + " rows") : null),
    qualityCell(valueButton(facts.mine, "ev-value", here, "", finding.title, finding.column, facts.id)),
    qualityCell(valueButton(facts.theirs, "ev-value bad", there, sheet ? "sheet:" + sheet : "", finding.title, finding.column, facts.id),
      sheet ? node("small", "Sheet " + sheet) : null))));
}
// Ordered against received: two bars on one scale and how far apart they are.
function quantityTable(finding, examples) {
  return evidenceTable(["Row", "Record", "Ordered", "Received", "", "Off by"], examples.map(example => {
    const { facts } = example;
    const ordered = Number(facts.ordered), received = Number(facts.received);
    const top = Math.max(ordered, received) || 1;
    const ratio = received / ordered;
    const bars = el("div", "ev-bars", el("span", "ordered"), el("span", "received " + (ratio >= 5 ? "bad" : "warn")));
    bars.children[0].style.width = (100 * ordered / top) + "%";
    bars.children[1].style.width = (100 * received / top) + "%";
    const off = ratio >= 2 ? "×" + (Math.round(ratio * 10) / 10) : "+" + ((ratio - 1) * 100).toFixed(1) + "%";
    return el("tr", "", qualityCell(rowLink(example, finding, facts.row)), qualityCell(node("span", facts.id, "ev-id")),
      el("td", "num", number(facts.ordered)), el("td", "num", el("strong", "", number(facts.received))),
      el("td", "bars", bars), el("td", "num", pill(off, ratio >= 5 ? "bad" : "warn")));
  }));
}
// Dotted numbers: the two ways a reader can take them.
function readings(value) {
  if (!/^\d{1,3}(\.\d{3})+$/.test(value)) return null;
  return number(value.replace(/\./g, "")) === number(value) ? null :
    number(value) + " or " + number(value.replace(/\./g, ""));
}
function valueChips(finding, examples) {
  return el("div", "ev-chips", ...examples.map(example => {
    const { facts } = example;
    const chip = node("button", null, "ev-chip");
    chip.type = "button";
    chip.append(node("span", facts.value, "value"));
    const note = facts.times ? "×" + facts.times + " rows" : facts.first ? "Same as row " + facts.first :
      facts.line ? "Line " + facts.line : facts.file ? fileName(facts.file) : facts.row ? "Row " + facts.row : "";
    if (facts.row && facts.first) chip.firstChild.textContent = "Row " + facts.row;
    if (note) chip.append(node("small", [example.columnLabel, note].filter(Boolean).join(" · ")));
    const reading = readings(facts.value);
    if (reading) chip.append(node("em", reading));
    const source = facts.file || example.source;
    if (source) {
      chip.title = [source, locatorLabel(example.locator)].filter(Boolean).join(" · ");
      chip.onclick = () => openFile(source, facts.file ? "" : example.locator, finding.title, example.column, example.find);
    } else {
      chip.disabled = true;
    }
    return chip;
  }));
}
const EVIDENCE = { change: changeTable, compare: compareTable, quantity: quantityTable, value: valueChips };
function evidence(finding) {
  const shaped = finding.examples.filter(example => EVIDENCE[example.shape]);
  const blocks = [];
  for (const shape of Object.keys(EVIDENCE)) {
    const examples = shaped.filter(example => example.shape === shape);
    if (examples.length) blocks.push(EVIDENCE[shape](finding, examples));
  }
  const plain = finding.examples.filter(example => !EVIDENCE[example.shape]);
  if (plain.length) {
    blocks.push(el("ul", "examples", ...plain.map(example => el("li", "", node("span", example.text), " ",
      rowLink(example, finding)))));
  }
  return blocks;
}
function findingCard(finding) {
  const [label, tone] = SEVERITY[finding.severity] || [finding.severity, "idle"];
  const place = placeOf(finding);
  const heading = node("h3", place ? null : finding.title);
  const title = el("div", "issue-title", pill(label, tone), heading,
    finding.count > 1 ? node("span", String(finding.count), "count-chip") : null);
  if (place) {
    const open = node("button", finding.title, "issue-open");
    open.type = "button";
    open.title = "Open " + [place.source, locatorLabel(place.locator)].filter(Boolean).join(" · ");
    heading.append(open);
    title.classList.add("opens");
    title.onclick = () => openPlace(place, finding.title);
  }
  const text = el("div", "issue-text", title, finding.detail ? node("p", finding.detail) : null);
  const card = el("article", "issue " + finding.severity, el("header", "", text, fileBlock(finding)), ...evidence(finding));
  const shown = finding.examples.length;
  if (finding.count && shown && finding.count > shown) {
    card.append(el("small", "sub", (finding.count - shown) + " more in ",
      fileButton(finding.source || finding.examples[0].source, finding.locator, fileName(finding.source || finding.examples[0].source), finding.title,
        finding.check === "format" || finding.columns?.length > 1 ? null : finding.column)));
  }
  const linked = new Set(finding.examples.flatMap(example => [example.source, example.facts?.master, example.facts?.here, example.facts?.file,
    example.facts?.there?.replace(/ \[[^\]]+\]$/, "")]));
  const related = finding.related.filter(path => !linked.has(path));
  if (related.length) {
    const also = el("small", "sub", "Also in ");
    related.forEach((path, index) => {
      if (index) also.append(", ");
      also.append(fileButton(path, "", fileName(path), finding.title));
    });
    card.append(also);
  }
  return card;
}
// One card for the same problem in several columns of one file: each value keeps its column.
function merged(findings) {
  const cards = [];
  for (const finding of findings) {
    const key = [finding.severity, finding.title, finding.source, finding.sheet, finding.check].join("|");
    const tagged = finding.examples.map(example => example.facts && finding.column ? { ...example, columnLabel: finding.column } : example);
    const same = cards.find(card => card.key === key && card.examples.every(example => example.shape === "value"));
    if (same && tagged.every(example => example.shape === "value")) {
      same.columns.push(finding.column);
      same.examples.push(...tagged);
      same.count = (same.count || 0) + (finding.count || 0) || null;
      same.related = [...new Set([...same.related, ...finding.related])];
    } else {
      cards.push({ ...finding, key, columns: [finding.column], examples: tagged });
    }
  }
  return cards.map(card => card.columns.length > 1 ? { ...card, column: card.columns.join(", ") } :
    { ...card, examples: card.examples.map(({ columnLabel, ...example }) => example) });
}
function kindBlock(kind) {
  const total = kind.findings.length;
  const header = el("div", "row", el("h2", "", kind.title + " ", node("span", String(total), "count-chip")),
    node("small", kind.note, "meta"));
  return el("div", "kind", header, el("div", "issues", ...merged(kind.findings).map(findingCard)));
}
function folderBlock(folder) {
  const summary = el("summary", "", node("span", folder.folder, "mono"),
    node("small", folder.files.length + " file" + (folder.files.length === 1 ? "" : "s") + " · " +
      (folder.findings ? folder.findings + " with findings" : "no findings"), "meta"));
  const list = el("ul", "file-list", ...folder.files.map(file => el("li", file.findings ? "flagged" : "",
    fileButton(file.path, "", fileName(file.path)),
    file.findings ? pill(file.findings + " finding" + (file.findings === 1 ? "" : "s"), "warn") : null)));
  return el("details", "folder", summary, list);
}
// A summary tile that walks through the findings of its severity, one per click.
function jumpFigure(severity, label, value, tone, note) {
  const tile = figure(label, String(value), tone, note);
  if (!value) return tile;
  let next = 0;
  const go = () => {
    const cards = document.querySelectorAll("#quality-kinds .issue." + severity);
    if (!cards.length) return;
    const card = cards[next++ % cards.length];
    card.scrollIntoView({ behavior: "smooth", block: "center" });
    card.classList.remove("flash");
    void card.offsetWidth;
    card.classList.add("flash");
    card.querySelector(".issue-open, .source-link")?.focus({ preventScroll: true });
  };
  tile.classList.add("jump");
  tile.tabIndex = 0;
  tile.setAttribute("role", "button");
  tile.title = "Go to the next " + label.toLowerCase().replace(/s$/, "");
  tile.onclick = go;
  tile.onkeydown = event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); go(); } };
  return tile;
}
async function loadQuality() {
  qualityRequest?.abort();
  qualityRequest = new AbortController();
  const target = byId("quality-kinds");
  try {
    const response = await fetch("/api/quality", { signal: qualityRequest.signal });
    if (!response.ok) throw new Error("Read failed");
    const view = await response.json();
    if (!view.available) {
      byId("quality-summary").replaceChildren();
      target.replaceChildren(node("p", "No data source is connected, so there is nothing to check yet.", "empty"));
      byId("quality-files").replaceChildren();
      return;
    }
    const flagged = view.folders.reduce((sum, folder) => sum + folder.findings, 0);
    byId("quality-meta").textContent = view.files + " files and " + view.tables + " tables checked on " + view.as_of;
    byId("quality-summary").replaceChildren(
      jumpFigure("error", "Errors", view.counts.error, view.counts.error ? "bad" : "good", "Likely wrong"),
      jumpFigure("warn", "Warnings", view.counts.warn, view.counts.warn ? "warn" : "good", "May mislead"),
      jumpFigure("info", "Notes", view.counts.info, "", "Handled"),
      figure("Files with findings", flagged + " of " + view.files, ""));
    const found = view.kinds.filter(kind => kind.findings.length);
    const clean = view.kinds.filter(kind => !kind.findings.length).map(kind => kind.title.toLowerCase());
    target.replaceChildren(...found.map(kindBlock),
      clean.length ? node("p", "No findings for " + clean.join(", ") + ".", "meta foot") : null);
    byId("quality-files").replaceChildren(...view.folders.map(folderBlock));
  } catch (error) {
    if (error.name !== "AbortError") target.replaceChildren(node("p", "Could not load. Try again.", "empty"));
  }
}
