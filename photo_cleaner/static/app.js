const state = {
  tab: "exact",
  status: null,
  groups: { exact: [], similar: [], other: [] },
  totals: { exact: 0, similar: 0, other: 0 },
  offset: { exact: 0, similar: 0, other: 0 },
  offsetStack: { exact: [], similar: [], other: [] },
  pageSize: 30,
  pageFiles: { exact: 0, similar: 0, other: 0 },
  pageCapped: { exact: false, similar: false, other: false },
  selected: new Set(),
  anchorId: null,
  browse: { kind: "all", offset: 0, total: 0, files: [] },
};

const HINTS = {
  exact:
    "Byte-identical files. Suggested copies are safe to remove after a glance. Drag a box to select several, or click the card (not the photo). The keeper stays unless you check it.",
  similar:
    "Visually similar stills, including screenshots that match a photo. Nothing is pre-checked. Drag a box or Shift-click a range. Distance is Hamming bits from the keeper.",
  other:
    "Screenshots and other non-camera stills that are similar to each other, plus a grid of leftovers. Drag a box to select, Shift-click a range, or Select all in grid. Click a photo to zoom.",
};

const PHASE_TO_STAGE = {
  starting: "scan",
  scanning: "scan",
  metadata: "metadata",
  hashing: "exact",
  similar: "similar",
  grouping: "similar",
  ready: "done",
  error: "error",
};

function stagesFromStatus(s) {
  const order = ["scan", "metadata", "exact", "similar"];
  const current = PHASE_TO_STAGE[s.phase] || "scan";
  const out = {};
  if (s.similar_ready || s.phase === "ready") {
    for (const stage of order) out[stage] = "done";
    return out;
  }
  if (s.phase === "error") {
    out.scan = "done";
    out.metadata = "done";
    out.exact = s.exact_ready ? "done" : "wait";
    out.similar = "wait";
    return out;
  }
  const here = order.indexOf(current);
  for (const stage of order) {
    const idx = order.indexOf(stage);
    if (idx === here) out[stage] = "active";
    else if (idx < here) out[stage] = "done";
    else out[stage] = "wait";
  }
  if (s.exact_ready) out.exact = "done";
  return out;
}

function readinessCopy(s) {
  if (s.phase === "error") return s.message || "Scan failed.";
  if (s.similar_ready || s.phase === "ready") return "All three tabs are ready to review.";
  if (s.exact_ready) {
    return "Exact is ready — you can review it now. Similar and Other are still processing in the background.";
  }
  if (s.phase === "hashing") {
    return "Still hashing. Exact unlocks when SHA-256 finishes. Similar and Other wait after that.";
  }
  if (s.phase === "metadata") {
    return "Reading camera metadata. Exact, Similar, and Other are not ready yet.";
  }
  return "Indexing in the background. Tabs unlock as each stage finishes.";
}

const PHASE_SPAN = {
  starting: [0, 0.04],
  scanning: [0.02, 0.16],
  metadata: [0.18, 0.22],
  hashing: [0.4, 0.22],
  similar: [0.62, 0.3],
  grouping: [0.92, 0.07],
  ready: [1, 0],
  error: [0, 0],
};

function overallPct(s) {
  if (s.similar_ready || s.phase === "ready") return 100;
  const [base, span] = PHASE_SPAN[s.phase] || [0.05, 0.1];
  const local = s.progress_total > 0 ? s.progress_current / s.progress_total : 0.12;
  return Math.min(99, Math.round((base + span * local) * 100));
}

const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "html") node.innerHTML = value;
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2), value);
    } else if (key === "checked") node.checked = !!value;
    else node.setAttribute(key, value);
  }
  for (const kid of kids) {
    if (kid != null && kid !== false) node.append(kid);
  }
  return node;
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function formatWaste(label, bytes) {
  if (!bytes) return label;
  return `${label} · ${formatBytes(bytes)} recoverable`;
}

function formatBytes(n) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return i === 0 ? `${n} B` : `${v.toFixed(1)} ${units[i]}`;
}

function renderStatus(s) {
  state.status = s;
  $("root-path").textContent = s.root || "";
  $("scan-phase").textContent = s.phase || "";
  $("scan-msg").textContent = s.message || "";
  const done = s.similar_ready || s.phase === "ready";
  const failed = s.phase === "error";
  const busy = !done && !failed;
  const pct = overallPct(s);
  $("scan-bar").style.width = `${pct}%`;
  $("scan-pct").textContent = done
    ? "100%"
    : s.progress_total > 0
      ? `${pct}% overall`
      : `${pct}%`;
  $("work-label").textContent = failed ? "Failed" : done ? "Ready" : "Working";
  $("work-ready").textContent = readinessCopy(s);

  const work = $("work");
  work.classList.toggle("is-busy", busy);
  work.classList.toggle("is-done", done);
  work.classList.toggle("is-error", failed);
  work.setAttribute("aria-busy", busy ? "true" : "false");
  document.body.classList.toggle("is-busy", busy);

  const stages = stagesFromStatus(s);
  work.querySelectorAll("[data-stage]").forEach((node) => {
    node.classList.remove("is-active", "is-done", "is-wait");
    node.classList.add(`is-${stages[node.dataset.stage] || "wait"}`);
  });

  $("count-exact").textContent = s.exact_ready ? String(s.exact_groups) : "…";
  $("count-similar").textContent = s.similar_ready ? String(s.similar_groups) : "…";
  $("count-other").textContent = s.similar_ready ? String(s.other_groups) : "…";
  $("tab-exact").classList.toggle("is-wait", !s.exact_ready);
  $("tab-exact").classList.toggle("is-ready", !!s.exact_ready);
  $("tab-similar").classList.toggle("is-wait", !s.similar_ready);
  $("tab-similar").classList.toggle("is-ready", !!s.similar_ready);
  $("tab-other").classList.toggle("is-wait", !s.similar_ready);
  $("tab-other").classList.toggle("is-ready", !!s.similar_ready);
}

function card(file, { group = null } = {}) {
  const selected = state.selected.has(file.id);
  const node = el("article", {
    class: `card${file.keep ? " is-keep" : ""}${selected ? " is-sel" : ""}`,
    "data-id": String(file.id),
  });
  if (file.keep) node.dataset.keep = "1";
  const check = el("input", {
    type: "checkbox",
    checked: selected,
    "aria-label": `Select ${file.name}`,
    onchange: (ev) => {
      toggleSel(file.id, ev.target.checked);
      node.classList.toggle("is-sel", ev.target.checked);
      state.anchorId = file.id;
    },
  });
  const pick = el("label", { class: "pick", title: "Select to trash" });
  pick.append(check);
  pick.addEventListener("click", (ev) => ev.stopPropagation());
  pick.addEventListener("mousedown", (ev) => ev.stopPropagation());
  const thumb = el("div", {
    class: "thumb",
    onclick: (ev) => {
      if (ev.target.closest(".pick")) return;
      if (ev.shiftKey) {
        ev.preventDefault();
        selectRangeTo(file.id);
        return;
      }
      if (ev.metaKey || ev.ctrlKey) {
        ev.preventDefault();
        const on = !state.selected.has(file.id);
        toggleSel(file.id, on);
        syncCard(node);
        state.anchorId = file.id;
        return;
      }
      openLightbox(file);
    },
  });
  const img = el("img", {
    alt: file.name,
    loading: "lazy",
    src: file.thumb,
    draggable: "false",
  });
  img.addEventListener("error", () => {
    img.remove();
    thumb.append(file.ext.replace(".", "").toUpperCase() || "FILE");
  });
  thumb.append(img, pick);
  const badges = el("div", { class: "badges" });
  if (file.keep) badges.append(el("span", { class: "badge keep" }, "keep"));
  badges.append(el("span", { class: `badge ${file.kind}` }, file.kind.replace("_", " ")));
  if (group && group.tab !== "exact" && file.distance != null) {
    badges.append(el("span", { class: "badge" }, `d=${file.distance}`));
  }
  node.append(
    thumb,
    el(
      "div",
      { class: "meta" },
      el("div", { class: "name", title: file.path }, file.name),
      el("div", { class: "path" }, file.relpath),
      badges,
      el(
        "div",
        { class: "row-actions" },
        el("span", {}, `${file.size_label}${file.shot_time ? " · " + file.shot_time : ""}`),
        el("button", { class: "ghost", type: "button", onclick: () => reveal(file.id) }, "Finder")
      )
    )
  );
  return node;
}

function toggleSel(id, on) {
  if (on) state.selected.add(id);
  else state.selected.delete(id);
  updateSel();
}

function syncCard(node) {
  const id = Number(node.dataset.id);
  const on = state.selected.has(id);
  node.classList.toggle("is-sel", on);
  const cb = node.querySelector("input[type=checkbox]");
  if (cb) cb.checked = on;
}

function syncAllCards() {
  document.querySelectorAll(".card[data-id]").forEach(syncCard);
  updateSel();
}

function visibleCardNodes() {
  return [...document.querySelectorAll(".card[data-id]")];
}

function selectRangeTo(id) {
  const ids = visibleCardNodes().map((n) => Number(n.dataset.id));
  const end = ids.indexOf(id);
  const start = state.anchorId != null ? ids.indexOf(state.anchorId) : -1;
  if (start < 0 || end < 0) state.selected.add(id);
  else {
    const lo = Math.min(start, end);
    const hi = Math.max(start, end);
    for (let i = lo; i <= hi; i++) state.selected.add(ids[i]);
  }
  state.anchorId = id;
  syncAllCards();
}

function selectGroupFiles(group) {
  for (const file of group.files) {
    if (!file.keep) state.selected.add(file.id);
  }
  $("select-suggested").checked = false;
  syncAllCards();
}

function selectVisible() {
  for (const node of visibleCardNodes()) {
    if (node.dataset.keep === "1") continue;
    state.selected.add(Number(node.dataset.id));
  }
  $("select-suggested").checked = false;
  syncAllCards();
}

function selectBrowseGrid() {
  for (const file of state.browse.files) state.selected.add(file.id);
  $("select-suggested").checked = false;
  syncAllCards();
}

function cardVisibleRect(node) {
  const r = node.getBoundingClientRect();
  const parent = node.parentElement;
  if (!parent) return r;
  const p = parent.getBoundingClientRect();
  const left = Math.max(r.left, p.left);
  const top = Math.max(r.top, p.top);
  const right = Math.min(r.right, p.right);
  const bottom = Math.min(r.bottom, p.bottom);
  if (right <= left || bottom <= top) return null;
  return { left, top, right, bottom };
}

function marqueeHits(box) {
  const hits = [];
  for (const node of visibleCardNodes()) {
    const r = cardVisibleRect(node);
    if (!r) continue;
    if (r.left < box.right && r.right > box.left && r.top < box.bottom && r.bottom > box.top) {
      hits.push(Number(node.dataset.id));
    }
  }
  return hits;
}

function swallowNextClick() {
  const swallow = (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    document.removeEventListener("click", swallow, true);
  };
  document.addEventListener("click", swallow, true);
  setTimeout(() => document.removeEventListener("click", swallow, true), 400);
}

function bindMarquee() {
  const skip = ".modal, .lightbox, header, .work, .tabs, .toolbar, .chips, button, select, a, input, .pick";
  document.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    if (e.target.closest(skip)) return;
    if (!e.target.closest("#board, #browse-wrap")) return;

    const startX = e.clientX;
    const startY = e.clientY;
    const startCard = e.target.closest(".card");
    const onThumb = !!e.target.closest(".thumb");
    const subtract = e.altKey;
    const origin = new Set(state.selected);
    let dragged = false;
    let box = null;

    const paint = (ev) => {
      const x = Math.min(startX, ev.clientX);
      const y = Math.min(startY, ev.clientY);
      const w = Math.abs(ev.clientX - startX);
      const h = Math.abs(ev.clientY - startY);
      Object.assign(box.style, { left: `${x}px`, top: `${y}px`, width: `${w}px`, height: `${h}px` });
      const hits = marqueeHits({ left: x, top: y, right: x + w, bottom: y + h });
      state.selected.clear();
      for (const id of origin) state.selected.add(id);
      for (const id of hits) {
        if (subtract) state.selected.delete(id);
        else state.selected.add(id);
      }
      syncAllCards();
    };

    const onMove = (ev) => {
      const dist = Math.hypot(ev.clientX - startX, ev.clientY - startY);
      if (!dragged && dist < 8) return;
      if (!dragged) {
        dragged = true;
        document.body.classList.add("is-marquee");
        box = el("div", { class: "marquee" });
        document.body.append(box);
      }
      ev.preventDefault();
      paint(ev);
    };

    const onUp = (ev) => {
      document.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      box?.remove();
      document.body.classList.remove("is-marquee");
      if (dragged) {
        swallowNextClick();
        const hover = document.elementFromPoint(ev.clientX, ev.clientY)?.closest(".card");
        if (hover) state.anchorId = Number(hover.dataset.id);
        return;
      }
      if (!startCard || onThumb) return;
      const id = Number(startCard.dataset.id);
      if (e.shiftKey || ev.shiftKey) selectRangeTo(id);
      else {
        toggleSel(id, !state.selected.has(id));
        syncCard(startCard);
        state.anchorId = id;
      }
    };

    document.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  });
}

function updateSel() {
  const n = state.selected.size;
  $("sel-count").textContent = `${n} selected`;
  $("trash-btn").disabled = n === 0;
}

function renderBoard() {
  $("tab-hint").textContent = HINTS[state.tab];
  const board = $("board");
  board.replaceChildren();
  const groups = state.groups[state.tab];
  if (!groups.length) {
    const ready =
      state.tab === "exact" ? state.status?.exact_ready : state.status?.similar_ready;
    board.append(
      el(
        "div",
        { class: ready ? "empty" : "empty is-wait" },
        ready
          ? "No groups in this tab."
          : state.tab === "exact"
            ? "Exact is still hashing in the background…"
            : "This tab is still processing in the background…"
      )
    );
  } else {
    for (const group of groups) {
      board.append(
        el(
          "section",
          { class: "group" },
          el(
            "div",
            { class: "group-head" },
            el("span", {}, `${group.reason}${group.tab !== "exact" ? ` · max distance ${group.distance}` : ""}`),
            el(
              "span",
              { class: "group-tools" },
              el("span", {}, formatWaste(`${group.files.length} files`, group.waste_bytes)),
              el(
                "button",
                { class: "ghost", type: "button", onclick: () => selectGroupFiles(group) },
                "Select extras"
              )
            )
          ),
          el("div", { class: "cards" }, ...group.files.map((f) => card(f, { group })))
        )
      );
    }
  }
  const loaded = groups.length;
  const total = state.totals[state.tab];
  const offset = state.offset[state.tab];
  const start = loaded ? offset + 1 : 0;
  const end = offset + loaded;
  const files = state.pageFiles[state.tab] || 0;
  const capped = state.pageCapped[state.tab];
  if (!total) {
    $("page-info").textContent = "";
  } else if (capped) {
    $("page-info").textContent = `${start}–${end} of ${total} · ${files} images (500 max)`;
  } else {
    $("page-info").textContent = `${start}–${end} of ${total}`;
  }
  $("page-prev").hidden = offset <= 0;
  $("page-next").hidden = !loaded || end >= total;
  $("browse-wrap").hidden = state.tab !== "other";
  if (state.tab === "other") renderBrowse();
}

function renderBrowse() {
  const grid = $("browse-grid");
  grid.replaceChildren();
  for (const file of state.browse.files) grid.append(card(file));
  $("browse-total").textContent = `${state.browse.total} ungrouped`;
  $("browse-more").hidden = state.browse.files.length >= state.browse.total;
}

async function loadGroups(reset = false) {
  const tab = state.tab;
  if (reset) {
    state.offset[tab] = 0;
    state.offsetStack[tab] = [];
    state.groups[tab] = [];
  }
  const offset = state.offset[tab];
  const data = await api(
    `/api/groups?tab=${tab}&offset=${offset}&limit=${state.pageSize}&max_files=500`
  );
  state.totals[tab] = data.total;
  state.groups[tab] = data.groups;
  state.pageFiles[tab] = data.file_count ?? data.groups.reduce((n, g) => n + g.files.length, 0);
  state.pageCapped[tab] = !!data.capped;
  if ($("select-suggested").checked) selectSuggested(true);
  else renderBoard();
}

async function loadBrowse(reset = false) {
  if (reset) {
    state.browse.offset = 0;
    state.browse.files = [];
  }
  const kind = state.browse.kind;
  const data = await api(`/api/browse?kind=${kind}&offset=${state.browse.offset}&limit=60`);
  state.browse.total = data.total;
  state.browse.files = reset ? data.files : state.browse.files.concat(data.files);
  state.browse.offset = state.browse.files.length;
  renderBrowse();
}

function isSuggested(file) {
  return state.tab === "exact" ? !file.keep : !!file.suggested_delete;
}

function selectSuggested(on) {
  const groups = state.groups[state.tab];
  for (const group of groups) {
    for (const file of group.files) {
      if (on && isSuggested(file)) state.selected.add(file.id);
      else if (!on) state.selected.delete(file.id);
    }
  }
  if (state.tab === "other" && on === false) {
    for (const file of state.browse.files) state.selected.delete(file.id);
  }
  updateSel();
  renderBoard();
}

async function reveal(id) {
  await api("/api/reveal", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id }),
  });
}

function openLightbox(file) {
  const src = file.browser_preview ? file.file : file.thumb;
  $("lb-img").src = src;
  $("lb-img").alt = file.name;
  $("lb-meta").textContent = `${file.path} · ${file.size_label}`;
  $("lightbox").hidden = false;
}

function selectedFiles() {
  const found = [];
  const seen = new Set();
  for (const tab of ["exact", "similar", "other"]) {
    for (const group of state.groups[tab]) {
      for (const file of group.files) {
        if (state.selected.has(file.id) && !seen.has(file.id)) {
          found.push(file);
          seen.add(file.id);
        }
      }
    }
  }
  for (const file of state.browse.files) {
    if (state.selected.has(file.id) && !seen.has(file.id)) found.push(file);
  }
  return found;
}

function openModal() {
  const files = selectedFiles();
  $("modal-copy").textContent = `Move ${files.length} file${files.length === 1 ? "" : "s"} to Trash?`;
  const list = $("modal-list");
  list.replaceChildren();
  for (const file of files.slice(0, 40)) {
    const keepNote = file.keep ? " (marked keep)" : "";
    list.append(el("li", {}, `${file.relpath}${keepNote}`));
  }
  if (files.length > 40) list.append(el("li", {}, `…and ${files.length - 40} more`));
  $("modal").hidden = false;
}

async function confirmTrash() {
  const files = selectedFiles();
  $("modal-go").disabled = true;
  try {
    const result = await api("/api/trash", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids: files.map((f) => f.id), confirm: true }),
    });
    for (const file of result.files || []) state.selected.delete(file.id);
    $("modal").hidden = true;
    await refreshAfterTrash();
    if (result.errors?.length) {
      alert(`${result.trashed} moved. ${result.errors.length} failed:\n${result.errors.map((e) => e.error).join("\n")}`);
    }
  } catch (err) {
    alert(err.message);
  } finally {
    $("modal-go").disabled = false;
    updateSel();
  }
}

async function refreshAfterTrash() {
  const s = await api("/api/status");
  renderStatus(s);
  await loadGroups(true);
  if (state.tab === "other") await loadBrowse(true);
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  window.scrollTo({ top: 0, behavior: reduce ? "auto" : "smooth" });
}

async function poll() {
  try {
    const prev = state.status;
    const s = await api("/api/status");
    renderStatus(s);
    const exactJustReady = s.exact_ready && !prev?.exact_ready;
    const similarJustReady = s.similar_ready && !prev?.similar_ready;
    if (!prev) await loadGroups(true);
    else if (state.tab === "exact" && exactJustReady) await loadGroups(true);
    else if (state.tab !== "exact" && similarJustReady) {
      await loadGroups(true);
      if (state.tab === "other") await loadBrowse(true);
    }
    if (s.phase !== "ready" && s.phase !== "error") setTimeout(poll, 900);
    else if (!s.similar_ready) setTimeout(poll, 900);
  } catch {
    setTimeout(poll, 1500);
  }
}

function bind() {
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", async () => {
      document.querySelectorAll(".tab").forEach((b) => {
        const on = b === btn;
        b.classList.toggle("is-on", on);
        b.setAttribute("aria-selected", on ? "true" : "false");
      });
      $("review-panel").setAttribute("aria-labelledby", btn.id);
      state.tab = btn.dataset.tab;
      $("select-suggested").checked = false;
      $("suggest-text").textContent =
        state.tab === "exact" ? "Select suggested copies" : "Select likely extras";
      renderBoard();
      if (!state.groups[state.tab].length) await loadGroups(true);
      if (state.tab === "other" && !state.browse.files.length) await loadBrowse(true);
    });
  });
  $("select-suggested").addEventListener("change", (ev) => selectSuggested(ev.target.checked));
  $("select-visible").addEventListener("click", selectVisible);
  $("browse-select").addEventListener("click", selectBrowseGrid);
  $("clear-sel").addEventListener("click", () => {
    state.selected.clear();
    state.anchorId = null;
    $("select-suggested").checked = false;
    syncAllCards();
  });
  $("page-size").addEventListener("change", async (ev) => {
    state.pageSize = Number(ev.target.value) || 30;
    state.groups = { exact: [], similar: [], other: [] };
    state.offset = { exact: 0, similar: 0, other: 0 };
    state.offsetStack = { exact: [], similar: [], other: [] };
    await loadGroups(true);
  });
  $("page-prev").addEventListener("click", async () => {
    const tab = state.tab;
    state.offset[tab] = state.offsetStack[tab].pop() ?? 0;
    await loadGroups(false);
  });
  $("page-next").addEventListener("click", async () => {
    const tab = state.tab;
    const shown = state.groups[tab].length;
    if (!shown) return;
    state.offsetStack[tab].push(state.offset[tab]);
    state.offset[tab] += shown;
    await loadGroups(false);
  });
  $("browse-more").addEventListener("click", () => loadBrowse(false));
  $("trash-btn").addEventListener("click", openModal);
  $("modal-cancel").addEventListener("click", () => ($("modal").hidden = true));
  $("modal-go").addEventListener("click", confirmTrash);
  $("lb-close").addEventListener("click", () => ($("lightbox").hidden = true));
  $("lightbox").addEventListener("click", (ev) => {
    if (ev.target.id === "lightbox") $("lightbox").hidden = true;
  });
  document.querySelectorAll(".chip").forEach((chip) => {
    chip.addEventListener("click", async () => {
      document.querySelectorAll(".chip").forEach((c) => c.classList.toggle("is-on", c === chip));
      state.browse.kind = chip.dataset.kind;
      await loadBrowse(true);
    });
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") {
      $("lightbox").hidden = true;
      $("modal").hidden = true;
    }
  });
  bindMarquee();
}

bind();
updateSel();
poll();
