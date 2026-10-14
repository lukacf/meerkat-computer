const form = document.querySelector("#unlock-form");
const tokenInput = document.querySelector("#operator-token");
const toolbar = document.querySelector("#toolbar");
const labels = document.querySelector("#show-labels");
const refresh = document.querySelector("#refresh");
const status = document.querySelector("#status");
const grid = document.querySelector("#previews");
const cards = new Map();
let token = "";
let generation = 0;
let timer;
let controller;
let busy = false;

function clearFrame(card, message) {
  card.image.removeAttribute("src");
  card.image.hidden = true;
  card.placeholder.textContent = message;
  card.placeholder.hidden = false;
  card.capture.textContent = "No screenshot is being shown.";
  card.observation = null;
}
function lock(message = "Locked. Unlock to view your computers.") {
  generation += 1;
  token = "";
  tokenInput.value = "";
  controller?.abort();
  clearTimeout(timer);
  cards.clear();
  grid.replaceChildren();
  form.hidden = false;
  toolbar.hidden = true;
  busy = false;
  refresh.disabled = false;
  status.textContent = message;
}
async function get(path, signal, credential) {
  const response = await fetch(path, { signal, headers: { Authorization: `Bearer ${credential}` }, credentials: "omit", cache: "no-store", redirect: "error", mode: "same-origin", referrerPolicy: "no-referrer" });
  if (!response.ok) throw new Error("Preview request failed");
  return response.json();
}
function element(tag, className, parent) {
  const node = document.createElement(tag);
  node.className = className;
  parent.append(node);
  return node;
}
function cardFor(computer) {
  let card = cards.get(computer.computer_id);
  if (!card) {
    const root = element("article", "preview-card", grid);
    const label = element("div", "labels", root);
    const name = element("h2", "", label);
    const scope = element("p", "scope", label);
    const area = element("div", "capture", root);
    const image = element("img", "", area);
    image.alt = "Agent screenshot";
    image.hidden = true;
    const placeholder = element("p", "placeholder", area);
    const info = element("div", "capture-info", root);
    const state = element("div", "computer-state", info);
    const capture = element("div", "", info);
    card = { root, label, name, scope, image, placeholder, state, capture, observation: null };
    image.addEventListener("error", () => clearFrame(card, "This capture could not be displayed. Waiting for the next agent screenshot."));
    clearFrame(card, "Waiting for an agent screenshot.");
    cards.set(computer.computer_id, card);
  }
  const owner = computer.owner || {};
  card.name.textContent = owner.kind === "identity" ? String(owner.identity || "Computer") : "Session computer";
  card.scope.textContent = [owner.realm, owner.namespace || owner.session_id, computer.slot, computer.profile].filter(Boolean).join(" / ");
  card.label.hidden = !labels.checked;
  card.state.textContent = String(computer.phase || "Unknown state");
  return card;
}
function png(data, width, height) {
  if (typeof data !== "string" || data.length > 10666668 || data.length % 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(data)) return false;
  try {
    const bytes = atob(data);
    const numberAt = offset => ((bytes.charCodeAt(offset) * 16777216) + (bytes.charCodeAt(offset + 1) << 16) + (bytes.charCodeAt(offset + 2) << 8) + bytes.charCodeAt(offset + 3));
    return bytes.length >= 33 && bytes.length <= 8000000 && bytes.slice(0, 8) === "\x89PNG\r\n\x1a\n" && bytes.slice(12, 16) === "IHDR" && numberAt(16) === width && numberAt(20) === height;
  } catch { return false; }
}
function show(card, view) {
  if (view.status !== "available") {
    const messages = { private_input: "Private input is protected. Waiting for a safe agent capture.", operation_in_flight: "An agent operation is in progress. Waiting for its capture.", outcome_unknown: "The last operation has no confirmed outcome. Waiting for a new agent capture." };
    clearFrame(card, messages[view.reason] || "Waiting for a new agent screenshot.");
    return;
  }
  const frame = view.frame;
  if (!frame || !/^[a-f0-9]{32}$/.test(frame.observation_id) || !["browser", "desktop"].includes(frame.surface) || frame.mime_type !== "image/png" || !Number.isInteger(frame.width) || frame.width < 1 || frame.width > 3840 || !Number.isInteger(frame.height) || frame.height < 1 || frame.height > 2160 || !Number.isSafeInteger(frame.received_at_ms) || !Number.isSafeInteger(frame.age_ms) || frame.age_ms < 0) {
    clearFrame(card, "No valid screenshot is available.");
    return;
  }
  if (frame.data !== null) {
    if (!png(frame.data, frame.width, frame.height)) {
      clearFrame(card, "No valid screenshot is available.");
      return;
    }
    card.image.src = `data:image/png;base64,${frame.data}`;
    card.observation = frame.observation_id;
  } else if (card.observation !== frame.observation_id) {
    clearFrame(card, "Waiting for a new agent screenshot.");
    return;
  }
  card.image.hidden = false;
  card.placeholder.hidden = true;
  const seconds = Math.floor(frame.age_ms / 1000);
  const age = seconds < 60 ? `${seconds}s old` : `${Math.floor(seconds / 60)}m old`;
  card.capture.textContent = `Capture received ${new Date(frame.received_at_ms).toLocaleString()} - ${age}. Last agent screenshot, ${frame.surface}.`;
}
async function poll() {
  if (!token || busy) return;
  clearTimeout(timer);
  const current = generation;
  const credential = token;
  const pending = new AbortController();
  controller = pending;
  busy = true;
  refresh.disabled = true;
  try {
    const listing = await get("/v1/computers", pending.signal, credential);
    if (current !== generation) return;
    if (!Array.isArray(listing.computers)) throw new Error("Invalid computer list");
    const present = new Set();
    await Promise.all(listing.computers.map(async computer => {
      if (typeof computer.computer_id !== "string" || !/^[a-zA-Z0-9_-]{1,128}$/.test(computer.computer_id)) throw new Error("Invalid computer reference");
      present.add(computer.computer_id);
      const card = cardFor(computer);
      const after = card.observation ? `?after=${encodeURIComponent(card.observation)}` : "";
      const view = await get(`/v1/computers/${encodeURIComponent(computer.computer_id)}/preview${after}`, pending.signal, credential);
      if (current !== generation) return;
      if (view.computer?.computer_id !== computer.computer_id) throw new Error("Preview target changed");
      cardFor(view.computer);
      show(card, view);
    }));
    if (current !== generation) return;
    for (const [id, card] of cards) if (!present.has(id)) { card.root.remove(); cards.delete(id); }
    status.textContent = present.size ? "Checking saved captures every 2 seconds. Only agent activity produces a new screenshot." : "No computers are assigned yet.";
  } catch {
    if (current === generation) lock("Preview could not be read. Unlock again to reconnect.");
  } finally {
    if (current === generation) {
      busy = false;
      refresh.disabled = false;
      if (token) timer = setTimeout(poll, 2000);
    }
  }
}
form.addEventListener("submit", event => {
  event.preventDefault();
  const candidate = tokenInput.value;
  tokenInput.value = "";
  if (candidate.length < 32) { status.textContent = "Enter the full operator token."; return; }
  token = candidate;
  generation += 1;
  form.hidden = true;
  toolbar.hidden = false;
  status.textContent = "Reading saved captures...";
  void poll();
});
labels.addEventListener("change", () => { for (const card of cards.values()) card.label.hidden = !labels.checked; });
refresh.addEventListener("click", () => { void poll(); });
document.querySelector("#lock").addEventListener("click", () => lock());
window.addEventListener("pagehide", () => lock());
