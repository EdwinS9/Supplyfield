"use strict";

(() => {
  const panel = byId("source-progress"), picker = byId("source-file"), refresh = byId("refresh-sources");
  let files = [];

  function showFile() {
    const file = files[picker.selectedIndex];
    if (!file) return;
    const counts = Object.entries(file.claims).map(([status, n]) => n + " " + status);
    byId("source-counts").textContent = (counts.length ? "Reader claims: " + counts.join(", ") :
      "No reader claim cites this version") + " · Version " + file.sha256.slice(0, 12) +
      " · Observed " + file.changed_at;
    const proof = byId("source-proof");
    proof.replaceChildren();
    if (file.example_id) proof.append(sourceButton(file.example_id, null, "View reader evidence"));
    const link = node("a", "Open source version");
    link.href = "/api/evidence?" + new URLSearchParams({source: file.path, sha256: file.sha256});
    link.target = "_blank";
    link.rel = "noopener";
    proof.append(link);
  }

  async function load() {
    if (refresh.disabled) return;
    refresh.disabled = true;
    const selected = picker.value;
    byId("source-choices").hidden = true;
    const summary = byId("source-summary");
    summary.textContent = "Reading file versions and claims…";
    try {
      const response = await fetch("/api/source-progress", {cache: "no-store"});
      if (!response.ok) throw new Error();
      const data = await response.json();
      files = data.files;
      const read = files.filter(file => Object.keys(file.claims).length).length;
      const verified = files.filter(file => file.claims.verified > 0).length;
      summary.textContent = files.length
        ? read + " of " + files.length + " current file versions have reader claims. " +
          verified + (verified === 1 ? " has" : " have") + " at least one verified claim. Checked " + data.checked_at + "."
        : "No current file versions have been observed.";
      picker.replaceChildren(...files.map(file => new Option(file.path, file.path)));
      if (files.some(file => file.path === selected)) picker.value = selected;
      byId("source-choices").hidden = !files.length;
      showFile();
    } catch {
      summary.textContent = "Source processing status is unavailable. Refresh to try again.";
    } finally {
      refresh.disabled = false;
    }
  }

  picker.onchange = showFile;
  refresh.onclick = load;
  panel.ontoggle = () => { if (panel.open) load(); };
})();
