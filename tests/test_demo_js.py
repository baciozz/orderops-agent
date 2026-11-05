"""Execute UI delivery identity/error guards in a minimal DOM, not a browser."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_answer_identity_and_stale_guard():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional; browser QA remains separate")
    script = r"""
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const listeners = {};
const messages = {};
const element = (id) => ({
  dataset: {}, value: "", disabled: false, textContent: "",
  classList: {toggle() {}}, setAttribute() {},
  addEventListener(type, callback) { listeners[id + ":" + type] = callback; },
});
const ids = {};
["task-inputs", "task-input-message", "clarification-answer", "answer-text",
 "answer-message", "evidence-file", "upload-message"].forEach(id => { ids[id] = element(id); });
ids["task-inputs"].dataset = {taskId: "t1", intentRevision: "4", status: "NEEDS_CLARIFICATION"};
ids["evidence-file"].files = [];
let sources = [], currentStatus = "NEEDS_CLARIFICATION", fail = false, delay = false;
let calls = [], reloads = 0;
const document = {
  getElementById(id) { return ids[id] || null; },
  querySelectorAll(selector) {
    return selector.includes(":checked") ? sources : [];
  },
  addEventListener() {},
};
const context = {
  document, TextEncoder, Uint8Array, crypto: require("node:crypto").webcrypto,
  window: {location: {reload() { reloads++; }}, addEventListener() {}},
  fetch: async (url, options) => {
    if (url.endsWith("/view")) return {ok: true, json: async () => ({
      card: {intent_revision: 4, status: currentStatus},
    })};
    calls.push(JSON.parse(options.body));
    if (delay) await new Promise(resolve => setTimeout(resolve, 15));
    if (fail) throw new Error("synthetic network failure");
    return {ok: true, json: async () => ({status: "QUEUED"})};
  },
};
vm.runInNewContext(fs.readFileSync("app/web/static/demo.js", "utf8"), context);
const submit = () => listeners["clarification-answer:submit"]({preventDefault() {}});
const settle = () => new Promise(resolve => setTimeout(resolve, 40));
(async () => {
  ids["answer-text"].value = "answer one";
  fail = true; submit(); await settle();
  assert.equal(calls.length, 1);
  const first = calls[0].client_event_key;
  submit(); await settle();
  assert.equal(calls[1].client_event_key, first);
  ids["answer-text"].value = "answer two";
  submit(); await settle();
  assert.notEqual(calls[2].client_event_key, first);
  sources = [{value: "source-new"}];
  submit(); await settle();
  assert.notEqual(calls[3].client_event_key, calls[2].client_event_key);
  assert.deepEqual(calls[3].source_ids, ["source-new"]);
  fail = false; delay = true; submit(); submit(); await settle();
  assert.equal(calls.length, 5);
  assert.equal(reloads, 1);
  ids["evidence-file"].files = [{name: "replacement.txt"}];
  submit(); await settle();
  assert.equal(calls.length, 5);
  assert.match(ids["answer-message"].textContent, /尚未成功上传/);
  ids["evidence-file"].files = [];
  currentStatus = "CANCELLED";
  submit(); await settle();
  assert.equal(calls.length, 5);
  assert.match(ids["answer-message"].textContent, /任务已变化/);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    root = Path(__file__).resolve().parents[1]
    subprocess.run([node, "-e", script], cwd=root, check=True, capture_output=True, text=True)
