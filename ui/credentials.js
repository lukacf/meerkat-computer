// This operator page never gives raw credentials to the agent transcript.
// Its bearer token and draft inputs live only in this document's memory.
const byId = (id) => document.getElementById(id);
const elements = (tag, text, className) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = String(text);
  if (className) node.className = className;
  return node;
};

let operatorToken = "";
let session = 0;
let busy = false;
let credentials = [];
let requests = [];
let knownOwners = [];
let selected = new Set();
let editingId = null;
let ownerSequence = 0;
const activeFetches = new Set();
const phases = {
  pending: "Pending", in_flight: "In progress", delivered: "Delivered",
  unknown: "Unknown", cancelled: "Cancelled", expired: "Expired", failed: "Failed",
};
const outcomes = {
  in_flight: "Delivery is in progress. Refresh to check its status.",
  delivered: "The values were delivered to this login form.",
  unknown: "Delivery could not be confirmed. Check the computer before requesting input again.",
  cancelled: "This request was cancelled.",
  expired: "This request expired. The agent can request a new login.",
  failed: "The input was not delivered. The agent can inspect the form and request a new login.",
};

function notice(message = "", error = false) {
  byId("notice").textContent = message;
  byId("notice").classList.toggle("error", error);
  byId("credential-error").textContent = error && byId("credential-dialog").open ? message : "";
}

function clearSensitiveInputs() {
  document.querySelectorAll('input[type="password"]').forEach((input) => { input.value = ""; });
}

function lock(message = "") {
  session += 1;
  operatorToken = "";
  activeFetches.forEach((controller) => controller.abort());
  activeFetches.clear();
  clearSensitiveInputs();
  byId("credential-form").reset();
  byId("credential-dialog").close();
  credentials = [];
  requests = [];
  knownOwners = [];
  renderKnownOwners();
  selected.clear();
  byId("credentials").replaceChildren();
  byId("requests").replaceChildren();
  byId("request-history-list").replaceChildren();
  byId("request-history").open = false;
  byId("request-history").hidden = true;
  byId("request-history-count").textContent = "0";
  byId("request-count").textContent = "0";
  byId("owner-rows").replaceChildren();
  ownerSequence = 0;
  addOwnerRow();
  byId("workspace").hidden = true;
  byId("session-controls").hidden = true;
  byId("unlock-panel").hidden = false;
  setBusy(false);
  notice(message);
}

function setBusy(value) {
  busy = value;
  document.querySelectorAll("input, select").forEach((input) => { input.disabled = value; });
  document.querySelectorAll("button").forEach((button) => {
    if (button.id !== "lock") button.disabled = value || button.dataset.requiresRefresh === "true";
  });
}

async function api(path, method = "GET", body) {
  const currentSession = session;
  const controller = new AbortController();
  activeFetches.add(controller);
  try {
    const response = await fetch(`/v1/${path}`, {
      method,
      mode: "same-origin",
      credentials: "omit",
      cache: "no-store",
      referrerPolicy: "no-referrer",
      signal: controller.signal,
      headers: {
        Authorization: `Bearer ${operatorToken}`,
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    if (currentSession !== session) throw { sessionEnded: true };
    if (response.status === 401 || response.status === 403) {
      lock("Operator access was rejected. Enter a valid operator token to continue.");
      throw { sessionEnded: true };
    }
    // Do not render server errors: an upstream diagnostic could echo a secret.
    if (!response.ok) throw { status: response.status };
    const result = response.status === 204 ? {} : await response.json();
    if (currentSession !== session) throw { sessionEnded: true };
    return result;
  } catch (error) {
    if (currentSession !== session) throw { sessionEnded: true };
    throw error;
  } finally {
    activeFetches.delete(controller);
  }
}

async function refreshData() {
  const [library, pending, computers] = await Promise.all([api("credentials"), api("credential-requests"), api("computers")]);
  credentials = library.credentials;
  requests = pending.requests;
  const owners = new Map();
  computers.computers.forEach(({ owner }) => {
    if (owner.kind === "identity") owners.set(JSON.stringify([owner.realm, owner.namespace, owner.identity]), owner);
  });
  knownOwners = [...owners.values()].sort((a, b) => ownerLabel(a).localeCompare(ownerLabel(b)));
  renderKnownOwners();
  selected = new Set([...selected].filter((id) => credentials.some((item) => item.id === id)));
  renderCredentials();
  renderRequests();
}

async function perform(operation, success) {
  if (busy) return;
  const currentSession = session;
  setBusy(true);
  notice();
  try {
    await operation();
    if (currentSession === session && success) notice(success);
  } catch (error) {
    if (!error.sessionEnded && currentSession === session) {
      notice("The operation could not be confirmed. Refresh to check its status before trying again.", true);
    }
  } finally {
    if (currentSession === session) setBusy(false);
  }
}

function ownerLabel(owner) {
  return owner.kind === "identity"
    ? `${owner.realm} / ${owner.namespace} / ${owner.identity}`
    : `${owner.realm} / session ${owner.session_id}`;
}

function renderKnownOwners() {
  const picker = byId("known-agent");
  const placeholder = elements("option", "Choose an agent");
  placeholder.value = "";
  picker.replaceChildren(placeholder);
  knownOwners.forEach((owner, index) => {
    const option = elements("option", ownerLabel(owner));
    option.value = String(index);
    picker.append(option);
  });
}

function addKnownOwner() {
  const selection = byId("known-agent").value;
  if (selection === "" || busy) return;
  const owner = knownOwners[Number(selection)];
  if (!owner) return;
  if (selectedOwners().some((selected) => selected.realm === owner.realm
      && selected.namespace === owner.namespace && selected.identity === owner.identity)) return;
  const row = [...byId("owner-rows").children].find((candidate) =>
    [...candidate.querySelectorAll("input")].every((input) => !input.value.trim())) || addOwnerRow();
  for (const key of ["realm", "namespace", "identity"]) row.querySelector(`[data-owner="${key}"]`).value = owner[key];
  byId("known-agent").value = "";
}

function button(label, className, handler) {
  const node = elements("button", label, className);
  node.type = "button";
  node.addEventListener("click", handler);
  return node;
}

function visibleCredentials() {
  const realm = byId("filter-realm").value.trim();
  const namespace = byId("filter-namespace").value.trim();
  const identity = byId("filter-identity").value.trim();
  return credentials.filter((item) => {
    if (realm && item.realm !== realm) return false;
    if (!namespace && !identity) return true;
    return item.assignments.some((owner) => owner.kind === "identity"
      && (!realm || owner.realm === realm)
      && (!namespace || owner.namespace === namespace)
      && (!identity || owner.identity === identity));
  });
}

function renderCredentials() {
  const container = byId("credentials");
  container.replaceChildren();
  const visible = visibleCredentials();
  byId("credential-count").textContent = credentials.length;
  byId("selection-count").textContent = `${selected.size} selected`;
  if (!visible.length) container.append(elements("div", "No credentials match this view.", "empty-state"));
  visible.forEach((credential) => {
    const card = elements("article", undefined, "credential-card");
    card.classList.toggle("selected", selected.has(credential.id));
    const top = elements("div", undefined, "credential-top");
    const checkbox = elements("input");
    checkbox.type = "checkbox";
    checkbox.setAttribute("aria-label", `Select ${credential.label}`);
    checkbox.checked = selected.has(credential.id);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) selected.add(credential.id); else selected.delete(credential.id);
      card.classList.toggle("selected", checkbox.checked);
      byId("selection-count").textContent = `${selected.size} selected`;
    });
    const details = elements("div", undefined, "credential-details");
    details.append(
      elements("div", credential.label, "credential-title"),
      elements("div", credential.origin, "credential-origin"),
      elements("div", `${credential.realm} / Version ${credential.version}`, "credential-meta"),
    );
    const actions = elements("div", undefined, "credential-actions");
    actions.append(
      button("Replace", "button quiet", () => openEditor(credential)),
      button("Delete", "button quiet danger", () => perform(async () => {
        await api(`credentials/${encodeURIComponent(credential.id)}`, "DELETE");
        await refreshData();
      }, "Credential deleted. Existing website sessions may remain signed in.")),
    );
    top.append(checkbox, details, actions);
    const assignments = elements("div", undefined, "assignments");
    credential.assignments.forEach((owner) => {
      const chip = elements("span", undefined, "identity-chip");
      const label = ownerLabel(owner);
      chip.append(elements("span", label));
      const revoke = button("x", "chip-revoke", () => perform(async () => {
        await api("credentials/unassign", "POST", { credential_ids: [credential.id], owners: [owner] });
        await refreshData();
      }, "Assignment revoked. Existing website sessions may remain signed in."));
      revoke.setAttribute("aria-label", `Revoke ${label} from ${credential.label}`);
      chip.append(revoke);
      assignments.append(chip);
    });
    if (!credential.assignments.length) assignments.append(elements("span", "No identities assigned", "unassigned"));
    card.append(top, assignments);
    container.append(card);
  });
}

function field(labelText, options = {}) {
  const label = elements("label", labelText);
  const input = elements("input");
  input.type = options.type || "text";
  input.autocomplete = "off";
  input.spellcheck = false;
  input.setAttribute("autocapitalize", "none");
  if (options.required) input.required = true;
  if (options.placeholder) input.placeholder = options.placeholder;
  if (options.aria) input.setAttribute("aria-label", options.aria);
  label.append(input);
  return { label, input };
}

function addOwnerRow() {
  ownerSequence += 1;
  const index = ownerSequence;
  const row = elements("div", undefined, "owner-row");
  const heading = elements("div", undefined, "owner-heading");
  heading.append(elements("span", `Identity ${index}`));
  heading.append(button("Remove", "text-button", () => {
    row.remove();
    if (!byId("owner-rows").children.length) addOwnerRow();
  }));
  const realm = field("Realm", { aria: `Owner realm ${index}`, placeholder: "studio", required: true });
  const namespace = field("Namespace", { aria: `Owner namespace ${index}`, placeholder: "team", required: true });
  const identity = field("Identity", { aria: `Owner identity ${index}`, placeholder: "artist", required: true });
  realm.input.dataset.owner = "realm";
  namespace.input.dataset.owner = "namespace";
  identity.input.dataset.owner = "identity";
  row.append(heading, realm.label, namespace.label, identity.label);
  byId("owner-rows").append(row);
  return row;
}

function selectedOwners() {
  return [...byId("owner-rows").children].map((row) => ({
    kind: "identity",
    realm: row.querySelector('[data-owner="realm"]').value.trim(),
    namespace: row.querySelector('[data-owner="namespace"]').value.trim(),
    identity: row.querySelector('[data-owner="identity"]').value.trim(),
  }));
}

async function changeAssignments(action) {
  if (!selected.size) { notice("Select at least one credential first.", true); return; }
  if (!byId("assignment-form").reportValidity()) return;
  const owners = selectedOwners();
  const ids = [...selected];
  if (owners.some((owner) => credentials.some((item) => ids.includes(item.id) && item.realm !== owner.realm))) {
    notice("Credentials and identities must belong to the same realm. Select credentials from one realm.", true);
    return;
  }
  await perform(async () => {
    await api(`credentials/${action}`, "POST", { credential_ids: ids, owners });
    await refreshData();
  }, action === "assign" ? "Assignments saved." : "Assignments revoked. Existing website sessions may remain signed in.");
}

function openEditor(credential) {
  byId("credential-form").reset();
  byId("credential-error").textContent = "";
  editingId = credential?.id || null;
  byId("credential-dialog-title").textContent = credential ? "Replace credential" : "Add credential";
  byId("credential-help").textContent = credential
    ? "Enter the username and password again to replace this credential. Its assignments are retained."
    : "Save a login, then assign it to the identities that need it.";
  byId("credential-realm").value = credential?.realm || byId("filter-realm").value;
  byId("credential-realm").readOnly = Boolean(credential);
  byId("credential-label").value = credential?.label || "";
  byId("credential-origin").value = credential?.origin || "";
  byId("credential-dialog").showModal();
}

function closeEditor() {
  byId("credential-form").reset();
  editingId = null;
  byId("credential-dialog").close();
}

function requestPhase(request) {
  return request.phase === "pending" && request.expires_at_ms <= Date.now() ? "expired" : request.phase;
}

function renderRequests() {
  const container = byId("requests");
  const history = byId("request-history");
  const historyList = byId("request-history-list");
  container.replaceChildren();
  historyList.replaceChildren();
  const active = requests.filter((request) => ["pending", "in_flight"].includes(requestPhase(request)));
  const historicalCount = requests.length - active.length;
  byId("request-count").textContent = requests.filter((request) => requestPhase(request) === "pending").length;
  byId("request-history-count").textContent = historicalCount;
  history.hidden = historicalCount === 0;
  if (!historicalCount) history.open = false;
  if (!active.length) container.append(elements("div", "No pending login requests. New requests appear here when an agent needs secure input.", "empty-state"));
  requests.forEach((request) => {
    const card = elements("article", undefined, "request-card");
    card.id = `request-${request.id}`;
    card.classList.toggle("linked", location.hash.slice(1) === request.id);
    const phase = requestPhase(request);
    const heading = elements("div", undefined, "request-heading");
    heading.append(elements("span", request.target.origin, "request-origin"), elements("span", phases[phase] || "Unavailable", "phase"));
    card.append(heading, elements("div", `${ownerLabel(request.owner)} / Computer ${request.computer_id}`, "request-context"));
    if (phase !== "pending") {
      card.append(elements("p", outcomes[phase] || "This request cannot accept input.", "request-result"));
      (phase === "in_flight" ? container : historyList).append(card);
      return;
    }
    const form = elements("form", undefined, "request-form");
    form.autocomplete = "off";
    const username = field("Username", { required: Boolean(request.target.username_ref) });
    const secret = field("Password or code", { type: "password", required: true });
    if (!request.target.username_ref) username.label.hidden = true;
    const actions = elements("div", undefined, "request-buttons");
    const submit = elements("button", "Send securely", "button primary");
    submit.type = "submit";
    actions.append(submit, button("Cancel request", "button quiet", () => perform(async () => {
      secret.input.value = "";
      await api(`credential-requests/${encodeURIComponent(request.id)}/cancel`, "POST", {});
      await refreshData();
    }, "Request cancelled.")));
    const saveRow = elements("div", undefined, "request-save");
    const saveLabel = elements("label", undefined, "check-label");
    const save = elements("input");
    save.type = "checkbox";
    saveLabel.append(save, elements("span", "Save for this identity"));
    const label = field("Credential label");
    label.label.className = "save-label";
    label.label.hidden = true;
    save.addEventListener("change", () => {
      label.label.hidden = !save.checked;
      label.input.required = save.checked;
    });
    saveRow.append(saveLabel, label.label);
    // Session-owned computers can receive one-time values; durable assignment is identity-only.
    if (request.owner.kind !== "identity") saveRow.hidden = true;
    form.append(username.label, secret.label, actions, saveRow,
      elements("p", `Expires ${new Date(request.expires_at_ms).toLocaleString()}. Input goes only to this request.`, "request-expiry"));
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (busy) return;
      const body = {
        values: { username: request.target.username_ref ? username.input.value || null : null, secret: secret.input.value },
        save: save.checked && request.owner.kind === "identity" ? { label: label.input.value.trim() } : null,
      };
      secret.input.value = "";
      username.input.value = "";
      submit.dataset.requiresRefresh = "true";
      void perform(async () => {
        if (requestPhase(request) !== "pending") {
          renderRequests();
          throw { expired: true };
        }
        await api(`credential-requests/${encodeURIComponent(request.id)}/fulfill`, "POST", body);
        await refreshData();
      }, "Secure input submitted. The request status shows the delivery outcome.");
    });
    card.append(form);
    container.append(card);
  });
}

byId("unlock-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (busy) return;
  operatorToken = byId("operator-token").value.trim();
  byId("operator-token").value = "";
  session += 1;
  void perform(async () => {
    await refreshData();
    byId("unlock-panel").hidden = true;
    byId("session-controls").hidden = false;
    byId("workspace").hidden = false;
    const linked = requests.find((request) => request.id === location.hash.slice(1));
    const linkedCard = linked && byId(`request-${linked.id}`);
    if (linkedCard) {
      if (byId("request-history-list").contains(linkedCard)) byId("request-history").open = true;
      linkedCard.scrollIntoView({ block: "nearest" });
    }
  });
});
byId("lock").addEventListener("click", () => lock("Locked. This tab no longer holds an operator token."));
byId("refresh").addEventListener("click", () => perform(refreshData, "Credential library and requests refreshed."));
byId("add-credential").addEventListener("click", () => openEditor());
byId("close-dialog").addEventListener("click", closeEditor);
byId("cancel-dialog").addEventListener("click", closeEditor);
byId("credential-dialog").addEventListener("cancel", () => {
  byId("credential-form").reset();
  editingId = null;
});
byId("credential-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (busy) return;
  const draft = {
    realm: byId("credential-realm").value.trim(),
    label: byId("credential-label").value.trim(),
    origin: byId("credential-origin").value.trim(),
    username: byId("credential-username").value || null,
    secret: byId("credential-secret").value,
  };
  byId("credential-secret").value = "";
  byId("credential-username").value = "";
  const id = editingId;
  void perform(async () => {
    await api(id ? `credentials/${encodeURIComponent(id)}` : "credentials", id ? "PUT" : "POST", draft);
    closeEditor();
    await refreshData();
  }, "Credential saved.");
});
byId("assignment-form").addEventListener("submit", (event) => { event.preventDefault(); void changeAssignments("assign"); });
byId("unassign").addEventListener("click", () => changeAssignments("unassign"));
byId("add-owner").addEventListener("click", addOwnerRow);
byId("add-known-agent").addEventListener("click", addKnownOwner);
byId("clear-selection").addEventListener("click", () => { selected.clear(); renderCredentials(); });
["filter-realm", "filter-namespace", "filter-identity"].forEach((id) => byId(id).addEventListener("input", renderCredentials));
window.addEventListener("pagehide", () => lock());
addOwnerRow();
