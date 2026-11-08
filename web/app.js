"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  config: null, map: null, tile: "HIGH", busy: false,
  job: null, timer: null, result: null, renderedResult: null,
};
const activeStatuses = new Set(["queued", "building", "solving", "cancelling"]);
const commands = {
  NORTH: "North", EAST: "East", SOUTH: "South", WEST: "West",
  STOMP: "Stomp", POWER_UP: "Power Up",
};
const starts = { red: [3, 1], green: [5, 1], blue: [1, 1] };

function storage(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch { /* The editor also works when browser storage is disabled. */ }
  return null;
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function power(value) {
  return value == null ? "--" : typeof value === "number" ? value.toLocaleString() : value;
}

function showError(message) {
  $("error").textContent = message || "";
  $("error").hidden = !message;
}

async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error || `Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function fixed(row, column) {
  return state.config.fixed.some((cell) => cell.row === row && cell.column === column);
}

function validateMap(board) {
  if (!Array.isArray(board) || board.length !== 5) throw new Error("The map must have 5 rows.");
  const editable = new Set(state.config.tiles.map((tile) => tile.id));
  board.forEach((row, r) => {
    if (!Array.isArray(row) || row.length !== 7) throw new Error(`Row ${r + 1} must have 7 columns.`);
    row.forEach((tile, c) => {
      if (fixed(r, c) ? tile !== state.config.map[r][c] : !editable.has(tile)) {
        throw new Error(`Invalid tile at row ${r + 1}, column ${c + 1}. Cats, beds, and OOB cells cannot move.`);
      }
    });
  });
  return structuredClone(board);
}

function tileInfo(tile) {
  return state.config.tiles.find((item) => item.id === tile);
}

function renderBoard(container, board, editable, cats = {}, floors = null) {
  container.textContent = "";
  container.append(element("span"));
  for (let column = 1; column <= 7; column++) container.append(element("span", column, "axis"));
  board.forEach((row, r) => {
    container.append(element("span", r + 1, "axis"));
    row.forEach((tile, c) => {
      const info = tileInfo(tile);
      const cell = element(editable ? "button" : "div", undefined, `tile tile-${tile.toLowerCase().replaceAll("_", "-")}`);
      cell.dataset.row = r;
      cell.dataset.column = c;
      const locked = fixed(r, c);
      const label = (editable ? tile : tile.replace("_CAT", "_START")).replaceAll("_", " ");
      cell.title = `Row ${r + 1}, column ${c + 1}: ${label}`;
      cell.append(element("span", label, "tile-name"));
      if (info?.floors) {
        const remaining = floors ? floors[r][c] : info.floors;
        cell.append(element("span", `${remaining} floor${remaining === 1 ? "" : "s"}`, "tile-detail"));
        cell.classList.toggle("depleted", remaining === 0);
      } else if (locked) {
        cell.append(element("span", editable ? "Locked" : "", "tile-detail"));
      }
      if (editable) {
        cell.type = "button";
        cell.dataset.fixed = locked;
        cell.disabled = state.busy || locked;
        cell.classList.toggle("fixed", locked);
        cell.setAttribute("aria-label", `${cell.title}${locked ? ", locked" : ", paint selected tile"}`);
        cell.addEventListener("click", () => {
          if (state.busy || locked) return;
          state.map[r][c] = state.tile;
          mapChanged();
          $("editor-grid").querySelector(`[data-row="${r}"][data-column="${c}"]`).focus();
        });
      } else {
        Object.entries(cats).forEach(([color, cat]) => {
          if (cat.alive && cat.row === r + 1 && cat.column === c + 1) {
            const marker = element("span", `${color[0].toUpperCase()}:${cat.direction[0]}`, `cat-marker ${color}`);
            marker.title = `${color}: ${power(cat.power)} power, facing ${cat.direction.toLowerCase()}`;
            cell.append(marker);
          }
        });
      }
      container.append(cell);
    });
  });
}

function renderPalette() {
  $("palette").textContent = "";
  state.config.tiles.forEach((tile) => {
    const button = element("button", tile.label, `tile-${tile.id.toLowerCase()}`);
    button.type = "button";
    button.dataset.tile = tile.id;
    button.title = tile.description;
    button.setAttribute("aria-pressed", String(tile.id === state.tile));
    button.append(element("span", "", "count"));
    button.addEventListener("click", () => {
      state.tile = tile.id;
      $("palette").querySelectorAll("button").forEach((item) => {
        item.setAttribute("aria-pressed", String(item.dataset.tile === state.tile));
      });
    });
    $("palette").append(button);
  });
  updateCounts();
}

function updateCounts() {
  $("palette").querySelectorAll("button").forEach((button) => {
    const count = state.map.flat().filter((tile) => tile === button.dataset.tile).length;
    button.querySelector(".count").textContent = `${count} on map`;
  });
}

function setBusy(busy) {
  state.busy = busy;
  ["solve", "time-limit", "example", "clear", "import"].forEach((id) => { $(id).disabled = busy; });
  $("cancel").disabled = !busy;
  $("palette").querySelectorAll("button").forEach((button) => { button.disabled = busy; });
  $("editor-grid").querySelectorAll("button").forEach((button) => {
    button.disabled = busy || button.dataset.fixed === "true";
  });
  $("editor-grid").setAttribute("aria-busy", String(busy));
}

function mapChanged() {
  storage("kaiju-map-v1", JSON.stringify(state.map));
  storage("kaiju-job-v1", null);
  clearTimeout(state.timer);
  state.job = null;
  state.result = null;
  state.renderedResult = null;
  $("results").hidden = true;
  $("replay-panel").hidden = true;
  $("download").disabled = true;
  $("status").className = "status";
  $("status").textContent = "Ready. Choose a search limit, then find an optimal placement.";
  ["score", "bound", "cost", "elapsed"].forEach((id) => { $(id).textContent = "--"; });
  showError("");
  renderBoard($("editor-grid"), state.map, true);
  updateCounts();
}

function appendRow(body, values) {
  const row = element("tr");
  values.forEach((value) => row.append(element("td", value)));
  body.append(row);
}

function renderPlacement(result, groups) {
  const grid = $("placement-grid");
  renderBoard(grid, result.map, false);
  $("placement-count").textContent = result.commands.length
    ? `${result.commands.length} command${result.commands.length === 1 ? "" : "s"} highlighted`
    : "No commands required for this placement";
  grid.querySelectorAll(".tile").forEach((cell) => {
    const row = Number(cell.dataset.row) + 1;
    const column = Number(cell.dataset.column) + 1;
    const height = tileInfo(result.map[row - 1][column - 1])?.floors || 0;
    if (!height) return;
    const placed = groups.get(`${row}, ${column}`)?.floors || {};
    cell.classList.add("has-command-slots");
    cell.classList.toggle("has-commands", Object.keys(placed).length > 0);
    cell.querySelector(".tile-detail").remove();
    const floors = element("div", undefined, `command-floors${height === 1 ? " single-floor" : ""}`);
    for (let floor = height; floor >= 1; floor--) {
      const command = placed[floor];
      const floorName = height === 1 ? "Only" : floor === 2 ? "Top" : "Bottom";
      const slot = element("div", undefined, `command-slot${command ? " assigned" : ""}`);
      slot.dataset.floor = floor;
      slot.dataset.command = command || "NONE";
      slot.title = `Row ${row}, column ${column}, ${floorName.toLowerCase()} floor ${floor}: ${commands[command] || "leave empty"}`;
      slot.setAttribute("role", "group");
      slot.setAttribute("aria-label", slot.title);
      const label = element("span", floorName, "slot-floor");
      label.append(element("span", ` (${floor})`, "slot-number"));
      slot.append(label);
      const content = element("span", undefined, "slot-command");
      if (command) {
        const icon = element("img", undefined, "command-icon");
        icon.src = `/icons/${command.toLowerCase()}.png`;
        icon.alt = "";
        icon.width = 32;
        icon.height = 36;
        icon.decoding = "async";
        content.append(icon);
      }
      content.append(element("span", commands[command] || "None", "command-name"));
      slot.append(content);
      floors.append(slot);
    }
    cell.append(floors);
  });
}

function renderResult(result) {
  state.result = result;
  $("results").hidden = false;
  $("replay-panel").hidden = false;
  $("download").disabled = false;
  $("score").textContent = power(result.score);
  $("cost").textContent = `$${result.cost} / $${state.config.budget}`;
  const signature = JSON.stringify([result.map, result.commands]);
  if (signature === state.renderedResult) return;
  state.renderedResult = signature;
  $("commands").textContent = "";
  const groups = new Map();
  result.commands.forEach((command) => {
    const key = `${command.row}, ${command.column}`;
    if (!groups.has(key)) groups.set(key, { row: command.row, column: command.column, floors: {} });
    groups.get(key).floors[command.floor] = command.command;
  });
  renderPlacement(result, groups);
  const ordered = [...groups.values()].sort((a, b) => a.row - b.row || a.column - b.column);
  ordered.forEach((group) => {
    const tile = result.map[group.row - 1][group.column - 1];
    appendRow($("commands"), [
      `${group.row}, ${group.column}`, tileInfo(tile).label,
      tile === "POWER" ? "N/A" : commands[group.floors[2]] || "--", commands[group.floors[1]] || "--",
    ]);
  });
  if (!ordered.length) appendRow($("commands"), ["No commands needed", "--", "--", "--"]);
  $("cats").textContent = "";
  ["red", "green", "blue"].forEach((color) => {
    const cat = result.cats[color];
    appendRow($("cats"), [
      color[0].toUpperCase() + color.slice(1), cat.bed_turn === null ? "--" : `Turn ${cat.bed_turn}`,
      power(cat.power), !cat.alive ? "Eliminated" : cat.bed_turn !== null ? "Home" : "Still on map",
    ]);
  });
  renderReplay();
}

function renderReplay() {
  if (!state.result) return;
  const turn = Number($("turn").value);
  const frame = turn === 0 ? {
    cats: Object.fromEntries(Object.entries(starts).map(([color, [row, column]]) => [
      color, { row, column, alive: true, direction: "EAST", power: 0 },
    ])),
    floors: state.result.map.map((row) => row.map((tile) => tileInfo(tile)?.floors || 0)),
    events: ["All cats start facing east with zero power."],
  } : state.result.trace[turn - 1];
  $("turn-label").textContent = turn;
  $("previous").disabled = turn === 0;
  $("next").disabled = turn === state.config.turns;
  renderBoard($("replay-grid"), state.result.map, false, frame.cats, frame.floors);
  $("replay-summary").textContent = Object.entries(frame.cats).map(([color, cat]) =>
    `${color[0].toUpperCase() + color.slice(1)}: ${power(cat.power)}${cat.alive ? "" : " (eliminated)"}`
  ).join(" | ");
  $("events").textContent = "";
  (frame.events.length ? frame.events : ["No tile effects this turn."]).forEach((event) => {
    $("events").append(element("li", event));
  });
}

function renderJob(job) {
  setBusy(activeStatuses.has(job.status));
  $("cancel").disabled = !state.busy || job.status === "cancelling";
  $("bound").textContent = power(job.upper_bound);
  $("elapsed").textContent = `${job.seconds.toFixed(1)} s`;
  if (job.result) renderResult(job.result);
  let text = "Searching - best placement so far; optimality is not yet proved.";
  let style = "active";
  if (job.status === "queued" || job.status === "building") text = "Preparing the map and score bounds...";
  else if (job.status === "cancelling") text = "Stopping the search and keeping the best placement...";
  else if (job.status === "failed") {
    text = "Search failed. Any displayed placement is not proved optimal.";
    style = "unproven";
    showError(job.error || "The solver could not complete this search.");
  } else if (job.status === "cancelled") {
    text = "Stopped - best placement found, not proved optimal.";
    style = "unproven";
  } else if (job.status === "completed") {
    text = job.result.optimal ? "Optimal placement - maximum score proved." : "Time limit reached - best placement found, not proved optimal.";
    style = job.result.optimal ? "optimal" : "unproven";
  }
  $("status").className = `status ${style}`;
  $("status").textContent = text;
}

async function pollJob(id) {
  try {
    const job = await api(`/api/jobs/${id}`);
    if (state.job !== id) return;
    showError("");
    renderJob(job);
    if (activeStatuses.has(job.status)) state.timer = setTimeout(() => pollJob(id), 600);
  } catch (error) {
    if (state.job !== id) return;
    showError(`${error.message} ${error.status === 404 ? "" : "Reconnecting; the search may still be running."}`);
    if (error.status === 404) {
      storage("kaiju-job-v1", null);
      state.job = null;
      setBusy(false);
    } else state.timer = setTimeout(() => pollJob(id), 2000);
  }
}

function download(name, data) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
  const link = element("a");
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function initialize() {
  state.config = await api("/api/config");
  state.map = structuredClone(state.config.map);
  try {
    const saved = storage("kaiju-map-v1");
    if (saved) state.map = validateMap(JSON.parse(saved));
  } catch { /* An outdated saved map must not prevent opening the editor. */ }
  renderPalette();
  const savedJob = storage("kaiju-job-v1");
  mapChanged();
  setBusy(false);
  $("export-map").disabled = false;
  if (savedJob) {
    try {
      const job = await api(`/api/jobs/${savedJob}`);
      state.map = validateMap(job.map);
      storage("kaiju-map-v1", JSON.stringify(state.map));
      renderBoard($("editor-grid"), state.map, true);
      updateCounts();
      state.job = savedJob;
      storage("kaiju-job-v1", savedJob);
      renderJob(job);
      if (activeStatuses.has(job.status)) pollJob(savedJob);
    } catch (error) {
      if (error.status !== 404) showError(error.message);
    }
  }

  $("solve").addEventListener("click", async () => {
    showError("");
    setBusy(true);
    state.job = null;
    $("cancel").disabled = true;
    try {
      const value = $("time-limit").value;
      const job = await api("/api/solve", { map: state.map, seconds: value ? Number(value) : null });
      state.job = job.id;
      state.renderedResult = null;
      storage("kaiju-job-v1", job.id);
      $("cancel").disabled = false;
      pollJob(job.id);
    } catch (error) {
      showError(error.message);
      setBusy(false);
    }
  });
  $("cancel").addEventListener("click", async () => {
    $("cancel").disabled = true;
    try { await api(`/api/jobs/${state.job}/cancel`, {}); }
    catch (error) { showError(error.message); $("cancel").disabled = false; }
  });
  $("example").addEventListener("click", () => {
    state.map = structuredClone(state.config.map);
    mapChanged();
  });
  $("clear").addEventListener("click", () => {
    if (!window.confirm("Clear all editable tiles? Cats, beds, and OOB cells will stay in place.")) return;
    state.map = state.map.map((row, r) => row.map((tile, c) => fixed(r, c) ? tile : "EMPTY"));
    mapChanged();
  });
  $("import").addEventListener("click", () => $("map-file").click());
  $("map-file").addEventListener("change", async (event) => {
    const file = event.target.files[0];
    if (!file || state.busy) return;
    try {
      if (file.size > 65536) throw new Error("Map files must be smaller than 64 KB.");
      const data = JSON.parse(await file.text());
      state.map = validateMap(Array.isArray(data) ? data : data.map);
      mapChanged();
    } catch (error) { showError(`Could not load map: ${error.message}`); }
    finally { event.target.value = ""; }
  });
  $("export-map").addEventListener("click", () => download("kaiju-map.json", { map: state.map }));
  $("download").addEventListener("click", () => {
    if (state.result) download("kaiju-solution.json", state.result);
  });
  $("turn").addEventListener("input", renderReplay);
  ["previous", "next"].forEach((id) => $(id).addEventListener("click", () => {
    $("turn").value = Math.max(0, Math.min(state.config.turns, Number($("turn").value) + (id === "next" ? 1 : -1)));
    renderReplay();
  }));
}

initialize().catch((error) => {
  showError(`Could not start the editor: ${error.message}`);
  $("status").textContent = "The local server could not be reached. Reload this page after restarting it.";
});
