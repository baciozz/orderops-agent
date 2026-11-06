"""Exercise upload retry identity and evidence selection in Node, without a browser."""

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = r"""
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const {createHash, webcrypto} = require("node:crypto");
const source = fs.readFileSync("app/web/static/demo.js", "utf8");
const hash = value => createHash("sha256").update(value).digest("hex");
const expectedKey = (taskId, file) => hash(JSON.stringify([
  taskId, file.name, hash(file.bytes),
]));
const until = async predicate => {
  const deadline = Date.now() + 5000;
  while (!predicate()) {
    assert.ok(Date.now() < deadline, "UI did not reach the expected observable state");
    await new Promise(resolve => setTimeout(resolve, 1));
  }
};
function makeFile(name = "evidence.txt", bytes = [65, 0, 255, 66]) {
  const content = Uint8Array.from(bytes);
  return {
    name, size: content.length, bytes: content, lastModified: 1,
    async arrayBuffer() { return content.slice().buffer; },
  };
}
class Element {
  constructor(tag = "div", id = "") {
    Object.assign(this, {
      tag, id, dataset: {}, value: "", disabled: false, checked: false,
      textContent: "", children: [], listeners: {}, attributes: {}, files: [],
      classList: {toggle() {}},
    });
  }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  append(...children) {
    for (const child of children) {
      this.children.push(child);
      if (typeof child !== "string") child.parent = this;
    }
  }
  remove() {
    if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this);
  }
  querySelectorAll(selector) {
    const descendants = this.children.filter(child => typeof child !== "string")
      .flatMap(child => [child, ...child.querySelectorAll("*")]);
    return descendants.filter(child => {
      if (selector === "*") return true;
      if (selector === "[data-empty]") return Object.hasOwn(child.dataset, "empty");
      if (selector === "li") return child.tag === "li";
      if (selector.startsWith("input")) {
        return child.tag === "input" &&
          (!selector.includes("answer-source") || child.name === "answer-source") &&
          (!selector.includes(":checked") || child.checked);
      }
      return false;
    });
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
function server() {
  const receipts = new Map();
  return {
    receipts,
    async respond(body) {
      const key = body.get("client_upload_key");
      if (!receipts.has(key)) receipts.set(key, `source-${receipts.size + 1}`);
      return {ok: true, status: 200, json: async () => ({
        source_id: receipts.get(key), parser_status: "ok",
      })};
    },
  };
}
function page({taskId = "task-1", revision = 4, inputEventId = "input-1",
               backend = server(), existingSources = []} = {}) {
  const ids = {};
  for (const id of ["task-inputs", "task-input-message", "source-upload", "upload-message",
                    "evidence-file", "uploaded-sources", "upload-button", "answer-text",
                    "answer-message", "clarification-answer"]) {
    ids[id] = new Element(id === "evidence-file" ? "input" : "div", id);
  }
  ids["task-inputs"].dataset = {
    taskId, intentRevision: String(revision), inputEventId, status: "NEEDS_CLARIFICATION",
  };
  const list = ids["uploaded-sources"];
  const addSource = sourceId => {
    const item = new Element("li"), label = new Element("label"), input = new Element("input");
    Object.assign(input, {type: "checkbox", name: "answer-source", value: sourceId});
    label.append(input); item.append(label); list.append(item);
    return input;
  };
  existingSources.forEach(addSource);
  if (!existingSources.length) {
    const empty = new Element("li"); empty.dataset.empty = ""; list.append(empty);
  }
  const p = {
    ids, list, backend, posts: [], views: [], resets: 0, reloads: 0,
    currentRevision: revision, currentStatus: "NEEDS_CLARIFICATION",
    respond: body => backend.respond(body),
    inputs: () => list.querySelectorAll('input[name="answer-source"]'),
    select(file) {
      ids["evidence-file"].files = file ? [file] : [];
      ids["evidence-file"].value = file ? `C:\\fakepath\\${file.name}` : "";
      ids["evidence-file"].listeners.change();
    },
    submit() { ids["source-upload"].listeners.submit({preventDefault() {}}); },
    settle() { return until(() => !ids["upload-button"].disabled); },
  };
  ids["source-upload"].reset = () => {
    p.resets++;
    ids["evidence-file"].files = [];
    ids["evidence-file"].value = "";
  };
  const document = {
    getElementById(id) { return ids[id] || null; },
    querySelectorAll(selector) {
      if (selector === "button, input, textarea") {
        return [ids["upload-button"], ids["evidence-file"], ids["answer-text"], ...p.inputs()];
      }
      return selector.startsWith("input") ? list.querySelectorAll(selector) : [];
    },
    createElement(tag) { return new Element(tag); },
    addEventListener() {},
  };
  class FormData {
    constructor() { this.fields = new Map(); }
    append(name, value) { this.fields.set(name, value); }
    get(name) { return this.fields.get(name); }
  }
  vm.runInNewContext(source, {
    document, FormData, TextEncoder, Uint8Array, crypto: webcrypto,
    window: {location: {reload() { p.reloads++; }}, addEventListener() {}},
    fetch: async (url, options) => {
      if (url === `/tasks/${encodeURIComponent(taskId)}/view`) {
        p.views.push(options);
        return {ok: true, json: async () => ({card: {
          intent_revision: p.currentRevision, status: p.currentStatus,
        }})};
      }
      assert.equal(url, "/sources", "Unexpected request in offline UI test");
      assert.equal(options.method, "POST");
      p.posts.push(options.body);
      return p.respond(options.body);
    },
  });
  return p;
}
async function upload(p, file) {
  p.select(file); p.submit(); await p.settle();
  assert.equal(p.posts.length, 1);
  const body = p.posts[0];
  assert.equal(body.get("file"), file);
  assert.equal(body.get("task_id"), p.ids["task-inputs"].dataset.taskId);
  assert.equal(body.get("client_upload_key"), expectedKey(body.get("task_id"), file));
  return body.get("client_upload_key");
}
"""


def run_ui_script(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional; browser QA remains separate")
    program = (
        HARNESS
        + "\n(async () => {\n"
        + script
        + r"""
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    )
    result = subprocess.run(
        [node, "-e", program],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_upload_lost_ack_retry_and_reselection_do_not_duplicate_evidence():
    run_ui_script(r"""
const p = page(), file = makeFile();
p.respond = async body => {
  await p.backend.respond(body); // Commit succeeded, but its acknowledgement was lost.
  throw new Error("synthetic lost acknowledgement");
};
const key = await upload(p, file);
assert.equal(p.backend.receipts.size, 1);
assert.equal(p.inputs().length, 0);
assert.equal(p.resets, 0);
assert.equal(p.ids["evidence-file"].files[0], file);
assert.match(p.ids["upload-message"].textContent, /synthetic lost acknowledgement/);
p.respond = body => p.backend.respond(body);
p.submit(); await p.settle();
assert.equal(p.posts.length, 2);
assert.equal(p.posts[1].get("client_upload_key"), key);
assert.equal(p.backend.receipts.size, 1);
assert.equal(p.list.querySelectorAll("li").length, 1);
assert.equal(p.inputs()[0].value, "source-1");
assert.equal(p.inputs()[0].checked, false);
assert.equal(p.resets, 1);
assert.equal(p.ids["evidence-file"].files.length, 0);
assert.equal(p.ids["evidence-file"].value, "");
p.submit(); await p.settle(); // A reset form cannot silently repeat the upload.
assert.equal(p.posts.length, 2);
p.inputs()[0].checked = true;
p.select(makeFile()); // A new File object for the same exact file is the same delivery.
assert.equal(p.inputs()[0].checked, false);
p.submit(); await p.settle();
assert.equal(p.posts.length, 3);
assert.equal(p.posts[2].get("client_upload_key"), key);
assert.equal(p.list.querySelectorAll("li").length, 1);
assert.equal(p.inputs()[0].checked, false);
assert.equal(p.resets, 2);
assert.equal(p.reloads, 0);
""")


def test_upload_key_survives_reload_revision_and_input_event_changes():
    run_ui_script(r"""
const backend = server(), original = page({backend});
const key = await upload(original, makeFile());
const restored = page({backend, revision: 17, inputEventId: "input-later",
                       existingSources: ["source-1", "other-source"]});
restored.inputs().forEach(input => { input.checked = true; });
const reselected = makeFile(); reselected.lastModified = 999999;
assert.equal(await upload(restored, reselected), key);
assert.equal(restored.list.querySelectorAll("li").length, 2);
assert.deepEqual(restored.inputs().map(input => input.value), ["source-1", "other-source"]);
assert.ok(restored.inputs().every(input => !input.checked));
assert.equal(backend.receipts.size, 1);
assert.equal(restored.resets, 1);
""")


def test_upload_key_binds_exact_filename_content_and_task():
    run_ui_script(r"""
const initial = await upload(page(), makeFile("evidence.txt"));
const changedName = await upload(page(), makeFile("Evidence.txt"));
const changedBytes = await upload(page(), makeFile("evidence.txt", [65, 0, 254, 66]));
const changedTask = await upload(page({taskId: "task/other"}), makeFile("evidence.txt"));
assert.equal(new Set([initial, changedName, changedBytes, changedTask]).size, 4);
const composed = await upload(page(), makeFile("caf\u00e9.txt"));
const decomposed = await upload(page(), makeFile("cafe\u0301.txt"));
assert.notEqual(composed, decomposed, "Filename identity must not normalize Unicode");
""")


@pytest.mark.parametrize("pending_stage", ["read", "post"])
def test_pending_upload_blocks_double_submit(pending_stage):
    run_ui_script(
        f'const pendingStage = "{pending_stage}";\n'
        + r"""
const p = page(), file = makeFile();
let release, waiting = false;
if (pendingStage === "read") {
  file.arrayBuffer = async () => {
    waiting = true;
    await new Promise(resolve => { release = resolve; });
    return file.bytes.slice().buffer;
  };
} else {
  p.respond = async body => {
    waiting = true;
    await new Promise(resolve => { release = resolve; });
    return p.backend.respond(body);
  };
}
p.select(file); p.submit();
await until(() => waiting);
assert.equal(p.ids["evidence-file"].disabled, true);
assert.equal(p.ids["upload-button"].disabled, true);
p.submit(); p.submit();
assert.equal(p.views.length, 1);
assert.equal(p.posts.length, pendingStage === "post" ? 1 : 0);
release(); await p.settle();
assert.equal(p.posts.length, 1);
assert.equal(p.resets, 1);
assert.equal(p.ids["evidence-file"].disabled, false);
assert.equal(p.ids["answer-text"].disabled, false);
assert.equal(p.inputs().length, 1);
"""
    )


@pytest.mark.parametrize("invalid_kind", ["oversized", "suffix", "empty"])
def test_upload_size_suffix_and_empty_selection_guards(invalid_kind):
    run_ui_script(
        f'const invalidKind = "{invalid_kind}";\n'
        + r"""
const p = page();
const file = invalidKind === "empty" ? null : makeFile(
  invalidKind === "suffix" ? "evidence.txt.exe" : "evidence.txt",
);
if (invalidKind === "oversized") file.size = 5 * 1024 * 1024 + 1;
p.select(file); p.submit(); await p.settle();
assert.equal(p.views.length, 0);
assert.equal(p.posts.length, 0);
assert.equal(p.resets, 0);
if (file) {
  assert.equal(p.ids["evidence-file"].files[0], file);
  assert.match(p.ids["upload-message"].textContent, /5 MiB/);
}
"""
    )


@pytest.mark.parametrize("suffix", ["TXT", "mD", "XLSX"])
def test_upload_allows_supported_suffixes_at_size_boundary(suffix):
    run_ui_script(
        f'const filename = "evidence.{suffix}";\n'
        + r"""
const p = page(), file = makeFile(filename, new Uint8Array(5 * 1024 * 1024));
await upload(p, file);
assert.equal(p.inputs().length, 1);
assert.equal(p.resets, 1);
"""
    )


@pytest.mark.parametrize("changed_field", ["revision", "status"])
def test_upload_stale_task_guard_preserves_selected_file(changed_field):
    run_ui_script(
        f'const changedField = "{changed_field}";\n'
        + r"""
const p = page(), file = makeFile();
if (changedField === "revision") p.currentRevision++;
else p.currentStatus = "CANCELLED";
p.select(file); p.submit(); await p.settle();
assert.equal(p.views.length, 1);
assert.equal(p.posts.length, 0);
assert.equal(p.resets, 0);
assert.equal(p.ids["evidence-file"].files[0], file);
assert.match(p.ids["upload-message"].textContent, /任务已变化/);
"""
    )


@pytest.mark.parametrize("failure_kind", ["http", "json", "parser"])
def test_unconfirmed_upload_response_preserves_file_for_retry(failure_kind):
    run_ui_script(
        f'const failureKind = "{failure_kind}";\n'
        + r"""
const p = page(), file = makeFile();
p.respond = async () => ({
  ok: failureKind !== "http", status: failureKind === "http" ? 409 : 200,
  json: async () => {
    if (failureKind === "json") throw new Error("synthetic invalid JSON");
    return failureKind === "parser" ? {source_id: "unconfirmed", parser_status: "error"} :
      {error: {code: "UPLOAD_CONFLICT", message: "synthetic conflict"}};
  },
});
const key = await upload(p, file);
assert.equal(p.inputs().length, 0);
assert.equal(p.resets, 0);
assert.equal(p.ids["evidence-file"].files[0], file);
assert.equal(p.ids["upload-message"].attributes.role, "alert");
p.respond = body => p.backend.respond(body);
p.submit(); await p.settle();
assert.equal(p.posts.length, 2);
assert.equal(p.posts[1].get("client_upload_key"), key);
assert.equal(p.inputs().length, 1);
assert.equal(p.resets, 1);
"""
    )
