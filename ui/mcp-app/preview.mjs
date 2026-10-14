import { App } from "@modelcontextprotocol/ext-apps";

const app = new App({ name: "My Computer", version: "0.1.0" });
const image = document.querySelector("#image");
const placeholder = document.querySelector("#placeholder");
const status = document.querySelector("#status");
const age = document.querySelector("#age");
const refresh = document.querySelector("#refresh");
const label = document.querySelector("#label");
let active = false, closed = false, busy = false, generation = 0, timer;
let observation = null, computer = null;

function clear(message) {
  image.removeAttribute("src");
  image.hidden = true;
  placeholder.hidden = false;
  placeholder.textContent = message;
  age.textContent = "No screenshot is being shown.";
  observation = null;
}

function close() {
  closed = true;
  active = false;
  generation += 1;
  clearTimeout(timer);
  refresh.disabled = true;
  clear("Preview closed.");
  status.textContent = "Preview closed.";
}

function png(data, width, height) {
  if (typeof data !== "string" || data.length > 10666668 || data.length % 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(data)) return false;
  try {
    const bytes = atob(data);
    const numberAt = offset => ((bytes.charCodeAt(offset) * 16777216) + (bytes.charCodeAt(offset + 1) << 16) + (bytes.charCodeAt(offset + 2) << 8) + bytes.charCodeAt(offset + 3));
    return bytes.length >= 33 && bytes.length <= 8000000 && bytes.slice(0, 8) === "\x89PNG\r\n\x1a\n" && bytes.slice(8, 16) === "\0\0\0\rIHDR" && numberAt(16) === width && numberAt(20) === height;
  } catch { return false; }
}

function show(view) {
  if (!view || typeof view.computer_id !== "string" || !/^[a-zA-Z0-9_-]{1,128}$/.test(view.computer_id)) throw new Error("Invalid preview");
  if (computer !== null && computer !== view.computer_id) throw new Error("Computer changed");
  computer = view.computer_id;
  if (view.status !== "available" || view.reason !== null) {
    const messages = {
      private_input: "Private input is protected. Waiting for a safe agent capture.",
      operation_in_flight: "An agent operation is in progress. Waiting for its capture.",
      outcome_unknown: "The last operation has no confirmed outcome. Waiting for a new capture.",
    };
    clear(messages[view.reason] || "Waiting for a new agent screenshot.");
    return;
  }
  const frame = view.frame;
  if (!frame || !/^[a-f0-9]{32}$/.test(frame.observation_id) || !["browser", "desktop"].includes(frame.surface) || frame.mime_type !== "image/png" || !Number.isInteger(frame.width) || frame.width < 1 || frame.width > 3840 || !Number.isInteger(frame.height) || frame.height < 1 || frame.height > 2160 || !Number.isSafeInteger(frame.received_at_ms) || frame.received_at_ms < 0 || !Number.isSafeInteger(frame.age_ms) || frame.age_ms < 0) throw new Error("Invalid capture");
  if (frame.data !== null) {
    if (!png(frame.data, frame.width, frame.height)) throw new Error("Invalid PNG");
    image.src = `data:image/png;base64,${frame.data}`;
    observation = frame.observation_id;
  } else if (observation !== frame.observation_id) throw new Error("Missing capture");
  image.hidden = false;
  placeholder.hidden = true;
  age.textContent = `Last agent screenshot, ${frame.surface}. Received ${new Date(frame.received_at_ms).toLocaleString()} - ${Math.floor(frame.age_ms / 1000)}s old.`;
}

async function poll() {
  if (!active || busy || document.hidden) return;
  clearTimeout(timer);
  const current = generation;
  busy = true;
  refresh.disabled = true;
  let healthy = false;
  try {
    const result = await app.callServerTool({
      name: "computer_preview", arguments: observation ? { after: observation } : {},
    });
    if (!active || current !== generation) return;
    if (result.isError) throw new Error("Preview denied");
    show(result._meta?.["meerkat-computer/preview"]);
    status.textContent = "Checking saved captures every 2 seconds.";
    healthy = true;
  } catch {
    if (active && current === generation) {
      clear("Preview unavailable. Refresh to check access again.");
      status.textContent = "Preview unavailable.";
    }
  } finally {
    if (current === generation && active) {
      busy = false;
      refresh.disabled = false;
      if (healthy) timer = setTimeout(poll, 2000);
    }
  }
}

// Reopening an invocation must read current privacy state. Historical images
// and tool arguments never directly populate this preview.
app.ontoolresult = () => { void poll(); };
app.onteardown = async () => { close(); return {}; };
app.onclose = close;
app.onhostcontextchanged = context => {
  if (context.theme) document.documentElement.style.colorScheme = context.theme;
};
refresh.addEventListener("click", () => { void poll(); });
document.querySelector("#show-label").addEventListener("change", event => { label.hidden = !event.target.checked; });
image.addEventListener("error", () => clear("This screenshot could not be displayed."));
window.addEventListener("pagehide", close);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    generation += 1;
    busy = false;
    clearTimeout(timer);
    clear("Preview paused while hidden.");
  }
  else void poll();
});

async function connect() {
  try {
    await app.connect();
    if (closed) return;
    if (!app.getHostCapabilities()?.serverTools) {
      status.textContent = "Current preview is unavailable in this read-only view.";
    } else {
      active = true;
      void poll();
    }
  } catch {
    close();
    status.textContent = "Preview unavailable. An MCP Apps host is required.";
  }
}
void connect();
