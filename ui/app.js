/* ============================================================================
   ui/app.js -- the whole client. No build step, no framework, no dependency.
   Loaded by index.html as a deferred classic script, so the DOM is parsed
   before anything below runs and the element lookups at the bottom are safe.

   WHAT THIS FILE IS RESPONSIBLE FOR
     Requirement 7 AC7  conversation state lives here and nowhere else
     Requirement 7 AC8  that state goes out in the request body
     Requirement 5 AC7  a progress indicator naming the step in flight
     Requirement 6 AC1  every error response rendered as a chat message
     Requirement 4 AC5  the files-I-read disclosure, after every turn

   THE DOM CONTRACT
     index.html carries the authoritative contract in a comment at the top of
     the file: every id, the phase-switch strings, the message node shape, the
     step node shape. This file binds to exactly those ids, creates none of its
     own, and uses no class name that is not listed there.

   SECURITY -- WHY THERE IS NO innerHTML IN THIS FILE
     Two untrusted things reach this page: model output and repository file
     content the model quotes back. Both arrive as JSON strings and both are
     written with textContent, so a hostile repository has no path to markup
     execution. There is no innerHTML assignment, no insertAdjacentHTML, no
     eval, no Function constructor, and no HTML built from a template string
     anywhere below. Cross-site scripting through a repository is impossible by
     construction rather than filtered after the fact. `splitBlocks` and
     `inlineSpans` do the markdown-ish shaping on plain text, and every node
     they produce is built with document.createElement.

   TRANSPORT
     Same-origin relative URL. One Lambda behind a Function URL serves this
     page and answers /api/chat, so there is no CORS to configure and no
     absolute endpoint to keep in sync with a deploy.
   ========================================================================= */

const API = "/api/chat"; // same origin: the Lambda that served this page
const MAX_TURNS = 10; // Requirement 3 AC7
const MAX_BODY_BYTES = 8192; // Requirement 8 AC1 -- the server's 413 is a backstop
const STORAGE_KEY = "welcome-guide.v1";

/* Requirement 9 AC2. Set to the public source repository URL at publication.
   Empty means the footer link stays hidden, so the page never carries a tab
   stop that goes nowhere. */
const SOURCE_URL = "";

/* The step vocabulary, which must match agent/steps.py StepName member for
   member. Order is execution order. `hintMs` drives the in-flight prediction
   only -- the real durations come back on the response and replace it. */
const STEPS = [
  { name: "validating_url", label: "Checking the URL", hintMs: 100 },
  { name: "fetching_metadata", label: "Looking up the repository", hintMs: 400 },
  { name: "listing_files", label: "Listing files on the default branch", hintMs: 700 },
  { name: "selecting_files", label: "Choosing what to read", hintMs: 200 },
  { name: "reading_files", label: "Reading the key files", hintMs: 900 },
  { name: "building_context", label: "Assembling the context", hintMs: 200 },
  { name: "asking_nova", label: "Asking Nova Lite", hintMs: 3500 },
];

const STEP_LABELS = new Map(STEPS.map((step) => [step.name, step.label]));

/* The sender label, always the first child of a message node. */
const WHO = {
  user: "You",
  assistant: "Guide",
  error: "Problem",
  notice: "Note",
};

/* One input, two phases. The strings are the ones index.html names. */
const PHASE = {
  orientation: {
    label: "Public GitHub repository URL",
    send: "Start the tour",
    placeholder: "https://github.com/pallets/flask",
  },
  chat: {
    label: "Ask about this repository",
    send: "Send",
    placeholder: "Which file handles routing?",
  },
};

/* Client-side copy. Every string states the cause first and names one next
   action last, the same shape as the server's ERROR_COPY table, because
   Requirement 6 AC1 applies to a failure that never reached the server too. */
const COPY = {
  unreachable:
    "I could not reach the agent. That is usually the network rather than " +
    "the repository, so check your connection and send that again.",
  unreadable:
    "The agent answered with something I could not read. Send that again, " +
    "and if it keeps happening the deployment is likely mid-update.",
  empty:
    "The agent answered with an empty message. Ask that again, and it should " +
    "come back with something to read.",
};

function trimmedCopy(dropped) {
  const turns = dropped === 1 ? "turn" : "turns";
  return (
    `I trimmed the earliest ${dropped} ${turns} of our conversation to stay ` +
    "inside the request size limit. Ask me to recap if I lose the thread."
  );
}

// ---------------------------------------------------------------------------
// Elements. The complete set this file touches; see the contract in index.html.
// ---------------------------------------------------------------------------

const ui = {
  composer: document.getElementById("composer"),
  input: document.getElementById("input"),
  label: document.getElementById("input-label"),
  send: document.getElementById("send"),
  transcript: document.getElementById("transcript"),
  status: document.getElementById("status"),
  steps: document.getElementById("steps"),
  ingested: document.getElementById("ingested"),
  ingestedList: document.getElementById("ingested-list"),
  sourceLink: document.getElementById("source-link"),
};

// ---------------------------------------------------------------------------
// State. Requirement 7 AC7: the conversation lives in the client.
// ---------------------------------------------------------------------------

const state = {
  repoUrl: null,
  messages: [], // [{role: "user" | "assistant", content: string}]
  ingestedPaths: [],
};

let inFlight = false;
let stepTimer = null;

const encoder = new TextEncoder();

/* Which phase the UI is in, derived rather than stored: the orientation is by
   definition the first assistant message of the session (Requirement 5 AC1),
   so its presence is the phase. One less field to keep consistent across a
   reload. */
function phase() {
  return state.messages.some((turn) => turn.role === "assistant")
    ? "chat"
    : "orientation";
}

// ---------------------------------------------------------------------------
// The request body. Requirement 7 AC8.
// ---------------------------------------------------------------------------

function encodeBody(repoUrl, messages) {
  return JSON.stringify({ repo_url: repoUrl, messages });
}

function byteLength(text) {
  return encoder.encode(text).length;
}

/* Trim to the ten most recent turns, then shrink from the oldest end until the
   encoded body fits. Dropping only ever from the front is what keeps the sent
   array a contiguous suffix of `state.messages` in its original order
   (Property 17).

   The loop stops at one turn. A single turn larger than the cap on its own is
   left to the server's 413, which comes back through the same error path as
   every other failure -- shortening the user's own words to fit would be
   worse than telling them the message is too long. */
function buildBody() {
  const recent = state.messages.slice(-MAX_TURNS);
  let messages = recent;
  let body = encodeBody(state.repoUrl, messages);

  while (messages.length > 1 && byteLength(body) > MAX_BODY_BYTES) {
    messages = messages.slice(1);
    body = encodeBody(state.repoUrl, messages);
  }

  return { body, messages, dropped: recent.length - messages.length };
}

// ---------------------------------------------------------------------------
// Plain-text shaping. Markdown is deliberately not rendered; see the design's
// Component 6 note. Three rules, applied to text, producing nodes.
// ---------------------------------------------------------------------------

/* Blank lines separate paragraphs; triple-backtick fences become <pre>.
   Returns [{kind: "p" | "pre", text}]. An unclosed fence still yields its
   block -- the model occasionally drops the closing one, and showing the code
   beats showing the backticks. */
function splitBlocks(text) {
  const lines = String(text === null || text === undefined ? "" : text)
    .replace(/\r\n?/g, "\n")
    .split("\n");

  const blocks = [];
  let buffer = [];
  let fenced = false;

  const flushProse = () => {
    const prose = buffer.join("\n");
    buffer = [];
    for (const chunk of prose.split(/\n[ \t]*\n+/)) {
      const trimmed = chunk.trim();
      if (trimmed !== "") blocks.push({ kind: "p", text: trimmed });
    }
  };

  const flushCode = () => {
    const code = buffer.join("\n");
    buffer = [];
    if (code.trim() !== "") blocks.push({ kind: "pre", text: code.replace(/\n+$/, "") });
  };

  for (const line of lines) {
    if (/^[ \t]*```/.test(line)) {
      if (fenced) flushCode();
      else flushProse();
      fenced = !fenced; // the info string on the fence line is dropped
      continue;
    }
    buffer.push(line);
  }

  if (fenced) flushCode();
  else flushProse();

  return blocks;
}

/* Paired single backticks become code spans. An unpaired backtick stays
   literal text rather than swallowing the rest of the paragraph.
   Returns [{code: boolean, text}]. */
function inlineSpans(text) {
  const source = String(text === null || text === undefined ? "" : text);
  const spans = [];
  let cursor = 0;

  while (cursor < source.length) {
    const open = source.indexOf("`", cursor);
    const close = open === -1 ? -1 : source.indexOf("`", open + 1);

    if (open === -1 || close === -1) {
      spans.push({ code: false, text: source.slice(cursor) });
      break;
    }

    if (open > cursor) spans.push({ code: false, text: source.slice(cursor, open) });
    spans.push({ code: true, text: source.slice(open + 1, close) });
    cursor = close + 1;
  }

  return spans.filter((span) => span.text !== "");
}

// ---------------------------------------------------------------------------
// Rendering. textContent only, everywhere.
// ---------------------------------------------------------------------------

function blockNode(block) {
  if (block.kind === "pre") {
    const pre = document.createElement("pre");
    pre.textContent = block.text;
    return pre;
  }

  const paragraph = document.createElement("p");
  for (const span of inlineSpans(block.text)) {
    if (span.code) {
      const code = document.createElement("code");
      code.textContent = span.text;
      paragraph.appendChild(code);
    } else {
      paragraph.appendChild(document.createTextNode(span.text));
    }
  }
  return paragraph;
}

/* Build a message node and append it to #transcript -- the ARIA live region --
   and nowhere else (Requirement 7 AC6, Property 19). */
function appendMessage(role, text) {
  const item = document.createElement("li");
  item.className = `msg msg--${role}`;

  const who = document.createElement("p");
  who.className = "msg__who";
  who.textContent = WHO[role] || WHO.notice;
  item.appendChild(who);

  const blocks = splitBlocks(text);
  if (blocks.length === 0) blocks.push({ kind: "p", text: String(text || "").trim() });
  for (const block of blocks) item.appendChild(blockNode(block));

  ui.transcript.appendChild(item);
  if (item.scrollIntoView) item.scrollIntoView({ block: "nearest" });
  return item;
}

/* Requirement 5 AC7. One short line in the live region, the step currently in
   flight. The whole checklist goes to #steps instead, so a screen reader is
   not handed seven items to announce. */
function setStatus(text, busy) {
  ui.status.textContent = text;
  if (busy) ui.status.classList.add("status--busy");
  else ui.status.classList.remove("status--busy");
}

function startSteps() {
  let index = 0;
  setStatus(STEPS[0].label, true);

  const advance = () => {
    stepTimer = null;
    index += 1;
    if (index >= STEPS.length) return;
    setStatus(STEPS[index].label, true);
    // Hold on the final step until the response lands: the prediction has run
    // out of vocabulary, and inventing an eighth step would be a lie.
    if (index < STEPS.length - 1) stepTimer = setTimeout(advance, STEPS[index].hintMs);
  };

  stepTimer = setTimeout(advance, STEPS[0].hintMs);
}

function stopSteps() {
  if (stepTimer !== null) clearTimeout(stepTimer);
  stepTimer = null;
  setStatus("", false);
}

function stepLabel(name) {
  return STEP_LABELS.get(name) || String(name === undefined ? "" : name);
}

function stepSuffix(status) {
  if (status === "ok") return "";
  if (status === "error") return " - failed";
  return ` - ${status}`;
}

function stepMs(value) {
  const ms = Number(value);
  return Number.isFinite(ms) ? Math.max(0, Math.round(ms)) : 0;
}

/* The authoritative record from the response, replacing the in-flight guess.
   Status reaches the user in words as well as the decorative CSS glyph. */
function renderSteps(steps) {
  const records = Array.isArray(steps) ? steps : [];
  const nodes = [];

  for (const record of records) {
    if (!record || typeof record !== "object") continue;

    const status = String(record.status === undefined ? "" : record.status);
    const known = status === "ok" || status === "error" || status === "skipped";

    const item = document.createElement("li");
    item.className = `step step--${known ? status : "error"}`;

    const name = document.createElement("span");
    name.className = "step__name";
    name.textContent = stepLabel(record.name) + stepSuffix(status);

    const ms = document.createElement("span");
    ms.className = "step__ms";
    ms.textContent = `${stepMs(record.ms)} ms`;

    item.appendChild(name);
    item.appendChild(ms);
    nodes.push(item);
  }

  ui.steps.replaceChildren(...nodes);
}

/* Requirement 4 AC5: what I read, visible without having to ask. An empty or
   absent list leaves the previous disclosure alone rather than blanking it --
   a failed turn did not un-read the files the last one read. */
function renderIngested(paths) {
  const clean = (Array.isArray(paths) ? paths : []).filter(
    (path) => typeof path === "string" && path !== "",
  );
  if (clean.length === 0) return;

  state.ingestedPaths = clean;
  save();

  const nodes = clean.map((path) => {
    const item = document.createElement("li");
    item.textContent = path;
    return item;
  });

  ui.ingestedList.replaceChildren(...nodes);
  ui.ingested.hidden = false;
}

/* One control, two phases: the label and the button text are the accessible
   names, so they are what has to change (Requirement 7 AC5). The placeholder
   moves with them so the box does not still suggest a URL mid-conversation. */
function applyPhase() {
  const copy = PHASE[phase()];
  ui.label.textContent = copy.label;
  ui.send.textContent = copy.send;
  ui.input.setAttribute("placeholder", copy.placeholder);
}

// ---------------------------------------------------------------------------
// sessionStorage mirror. Not localStorage: a shared or public browser should
// not hold on to someone else's tour, and the transcript is disposable.
// ---------------------------------------------------------------------------

function save() {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch (error) {
    // A disabled or full store costs the refresh-survives-reload nicety and
    // nothing else. Not worth a message to the user.
  }
}

function sanitizeTurns(value) {
  if (!Array.isArray(value)) return [];
  const turns = [];
  for (const turn of value) {
    if (!turn || typeof turn !== "object") continue;
    if (typeof turn.content !== "string" || turn.content === "") continue;
    turns.push({
      role: turn.role === "assistant" ? "assistant" : "user",
      content: turn.content,
    });
  }
  return turns;
}

function readSaved() {
  let raw = null;
  try {
    raw = sessionStorage.getItem(STORAGE_KEY);
  } catch (error) {
    return null;
  }
  if (!raw) return null;

  let parsed = null;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    return null;
  }
  if (!parsed || typeof parsed !== "object") return null;

  return {
    repoUrl: typeof parsed.repoUrl === "string" ? parsed.repoUrl : null,
    messages: sanitizeTurns(parsed.messages),
    ingestedPaths: (Array.isArray(parsed.ingestedPaths) ? parsed.ingestedPaths : []).filter(
      (path) => typeof path === "string" && path !== "",
    ),
  };
}

function restore() {
  const saved = readSaved();
  if (!saved) return;

  state.repoUrl = saved.repoUrl;
  state.messages = saved.messages;
  state.ingestedPaths = saved.ingestedPaths;

  // The URL submission is echoed into the transcript but is not a turn: the
  // server infers the orientation from an empty `messages` array, so putting
  // it in the history would turn a reload into a chat request.
  if (state.repoUrl) appendMessage("user", state.repoUrl);
  for (const turn of state.messages) appendMessage(turn.role, turn.content);
  renderIngested(state.ingestedPaths);
}

// ---------------------------------------------------------------------------
// The turn.
// ---------------------------------------------------------------------------

/* The response's message field. A string is the shape handler.py sends;
   `{role, content}` is the shape the design document's Wire Format section
   shows. Accepting both costs four lines and means the client does not have to
   be redeployed in lockstep with a change on that one field. */
function messageText(message) {
  if (typeof message === "string") return message.trim();
  if (message && typeof message === "object" && typeof message.content === "string") {
    return message.content.trim();
  }
  return "";
}

function errorText(error) {
  if (error && typeof error === "object" && typeof error.message === "string") {
    const message = error.message.trim();
    if (message !== "") return message;
  }
  return COPY.unreadable;
}

/* Every outcome comes back as a response envelope, including the ones that
   never reached the server, so there is exactly one render path for failure
   (Requirement 6 AC1). No quota branch and no retry countdown: with no usage
   plan in front of the function there is no 429 to render. */
async function post(body) {
  let response = null;
  try {
    response = await fetch(API, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body,
    });
  } catch (error) {
    return { error: { message: COPY.unreachable }, steps: [] };
  }

  try {
    const payload = await response.json();
    if (payload && typeof payload === "object") return payload;
  } catch (error) {
    // falls through to the unreadable envelope
  }

  return { error: { message: COPY.unreadable }, steps: [] };
}

function beginFlight() {
  inFlight = true;
  ui.send.disabled = true;
  ui.transcript.setAttribute("aria-busy", "true");
  ui.steps.replaceChildren();
  startSteps();
}

function endFlight() {
  inFlight = false;
  ui.send.disabled = false;
  ui.transcript.setAttribute("aria-busy", "false");
  stopSteps();
}

/* A failed turn must not leave a user message in the history with no answer
   after it: the next request would then carry two user turns in a row, which
   the Converse envelope rejects. The transcript keeps both the question and
   the failure -- what is dropped is the turn the model never saw. */
function rollbackLastUserTurn() {
  const last = state.messages[state.messages.length - 1];
  if (last && last.role === "user") {
    state.messages.pop();
    save();
  }
}

async function runTurn() {
  const { body, dropped } = buildBody();
  if (dropped > 0) appendMessage("notice", trimmedCopy(dropped));

  beginFlight();
  const payload = await post(body);
  endFlight();

  renderSteps(payload.steps);
  if (payload.repo && typeof payload.repo === "object") {
    renderIngested(payload.repo.ingested_paths);
  }

  const text = payload.error ? "" : messageText(payload.message);

  if (payload.error) {
    rollbackLastUserTurn();
    appendMessage("error", errorText(payload.error));
  } else if (text === "") {
    rollbackLastUserTurn();
    appendMessage("error", COPY.empty);
  } else {
    state.messages.push({ role: "assistant", content: text });
    save();
    appendMessage("assistant", text);
  }

  applyPhase();
  ui.input.focus(); // Requirement 7 AC4: focus comes back to the one input
}

function onSubmit(event) {
  if (event && event.preventDefault) event.preventDefault();
  if (inFlight) return;

  const text = ui.input.value.trim();
  if (text === "") {
    ui.input.focus();
    return;
  }
  ui.input.value = "";

  if (phase() === "orientation") {
    state.repoUrl = text;
  } else {
    state.messages.push({ role: "user", content: text });
  }
  save();
  appendMessage("user", text);

  void runTurn();
}

function onKeydown(event) {
  if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
  event.preventDefault();
  onSubmit(null);
}

// ---------------------------------------------------------------------------
// Wiring.
// ---------------------------------------------------------------------------

ui.composer.addEventListener("submit", onSubmit);
ui.input.addEventListener("keydown", onKeydown);

if (SOURCE_URL !== "") {
  ui.sourceLink.setAttribute("href", SOURCE_URL);
  ui.sourceLink.hidden = false;
}

restore();
applyPhase();
