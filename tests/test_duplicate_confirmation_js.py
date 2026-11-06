"""Node VM state controls, not real browser timing/visual verification."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def _sandbox_asyncio_polling():
    yield


def test_duplicate_checkbox_scoped_state():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable; browser QA remains separate")
    script = r"""
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
function setup({duplicate = true, renew = false, disabled = false} = {}) {
  const listeners = {}, ids = {}, calls = [];
  const element = id => ({
    dataset: {}, checked: true, disabled: false, textContent: "",
    isConnected: true, closest: () => ids["plan-actions"],
    classList: {toggle() {}}, setAttribute() {},
    addEventListener(type, fn) {listeners[id + ":" + type] = fn;},
  });
  for (const id of ["plan-actions", "approve", "commit", "action-message"])
    ids[id] = element(id);
  ids.approve.disabled = disabled;
  ids.commit.disabled = true;
  if (renew) ids.renew = element("renew");
  if (duplicate) {
    ids["acknowledge-duplicate"] = element("acknowledge-duplicate");
    ids["acknowledge-duplicate"].dataset = {taskId:"t1", planId:"p1", planHash:"h1"};
  }
  ids["plan-actions"].dataset = {
    taskId:"t1", planId:"p1", planHash:"h1", intentRevision:"2", approvalId:"",
  };
  let fail = false, delay = false, release, reloads = 0;
  const context = {
    document: {
      getElementById: id => ids[id] || null,
      querySelectorAll: () => [], addEventListener() {},
    },
    window: {
      addEventListener: (type, fn) => {listeners[type] = fn;},
      location: {reload() {reloads++;}},
    },
    fetch: async (url, options) => {
      calls.push({url, body:JSON.parse(options.body)});
      if (delay) await new Promise(resolve => {release = resolve;});
      return {ok:!fail, json:async () => fail ?
        {error:{code:"DUPLICATE_UNCONFIRMED", message:"confirm"}} :
        {approval_id:"a1", status:"ACTIVE", expires_at:"later"}};
    },
  };
  vm.runInNewContext(fs.readFileSync("app/web/static/demo.js", "utf8"), context);
  return {ids, calls, listeners, context,
    approve: () => listeners[(renew ? "renew" : "approve") + ":click"](),
    commit: () => listeners["commit:click"](),
    fail: value => {fail = value;}, delay: value => {delay = value;},
    release: () => release(), reloads: () => reloads,
  };
}
(async () => {
  let s = setup();
  const box = s.ids["acknowledge-duplicate"];
  assert.equal(box.checked, false, "initial browser-restored check is cleared");
  s.fail(true);
  await s.approve();
  assert.equal(s.calls[0].body.acknowledge_duplicate, undefined);
  assert.equal(s.calls.length, 1, "no auto retry with true");
  box.checked = true;
  await s.approve();
  assert.equal(s.calls[1].body.acknowledge_duplicate, true);
  assert.equal(box.checked, false, "error consumes confirmation");
  await s.approve();
  assert.equal(s.calls[2].body.acknowledge_duplicate, undefined);
  for (const key of ["taskId", "planId", "planHash"]) {
    const original = box.dataset[key];
    box.dataset[key] = "other"; box.checked = true;
    await s.approve();
    assert.equal(s.calls.at(-1).body.acknowledge_duplicate, undefined);
    box.dataset[key] = original;
  }
  for (const persisted of [false, true]) {
    box.checked = true;
    s.listeners.pageshow({persisted});
    assert.equal(box.checked, false);
  }
  box.checked = true; box.disabled = true;
  await s.approve();
  assert.equal(s.calls.at(-1).body.acknowledge_duplicate, undefined);
  box.disabled = false;
  for (const key of ["taskId", "planId", "planHash", "intentRevision"]) {
    const data = s.ids["plan-actions"].dataset, original = data[key];
    data[key] = "changed"; box.checked = true;
    const count = s.calls.length;
    await s.approve();
    assert.equal(s.calls.length, count);
    assert.equal(box.checked, false);
    data[key] = original;
  }
  s.fail(false); s.delay(true); box.checked = true;
  const pending = s.approve(); await s.approve();
  assert.equal(s.calls.at(-1).body.acknowledge_duplicate, true);
  const count = s.calls.length;
  s.release(); await pending;
  assert.equal(s.reloads(), 1);
  assert.equal(s.calls.filter(c => c.url.endsWith("/commit")).length, 0);
  await s.approve();
  assert.equal(s.calls.length, count, "success disables repeated approval until refresh");
  s.delay(false); await s.commit();
  assert.equal(s.calls.at(-1).url, "/plans/p1/commit");
  assert.equal(s.calls.at(-1).body.acknowledge_duplicate, undefined);
  await s.commit(); assert.equal(s.calls.length, count + 1);
  s = setup({duplicate:false}); await s.approve();
  assert.equal(s.calls[0].body.acknowledge_duplicate, undefined);
  s = setup({renew:true, disabled:true});
  assert.equal(s.ids["acknowledge-duplicate"].checked, false);
  s.ids["acknowledge-duplicate"].checked = true; await s.approve();
  assert.equal(s.calls[0].body.acknowledge_duplicate, true);
  s = setup({disabled:true});
  s.ids["acknowledge-duplicate"].checked = true; await s.approve();
  assert.equal(s.calls.length, 0);
  for (const renew of [false, true]) {
    s = setup({renew}); s.fail(true);
    const old = s.ids["acknowledge-duplicate"];
    old.checked = true; old.isConnected = false;
    await s.approve();
    assert.equal(s.calls.at(-1).body.acknowledge_duplicate, undefined,
      "a disconnected current lookup cannot confirm");
    assert.equal(old.checked, false);
    old.checked = true;
    const replacement = {...old, checked:true, isConnected:true};
    s.ids["acknowledge-duplicate"] = replacement;
    s.listeners.pageshow({persisted:false});
    assert.equal(old.checked, false, "reset clears detached original too");
    assert.equal(replacement.checked, false, "reset clears replacement too");
    old.checked = true;
    await s.approve();
    assert.equal(s.calls.at(-1).body.acknowledge_duplicate, undefined);
    assert.equal(old.checked, false);
    replacement.checked = true;
    await s.approve();
    assert.equal(s.calls.at(-1).body.acknowledge_duplicate, undefined,
      "replacement cannot inherit original rendered confirmation");
    assert.equal(replacement.checked, false);
    delete s.ids["acknowledge-duplicate"]; old.checked = true;
    await s.approve();
    assert.equal(s.calls.at(-1).body.acknowledge_duplicate, undefined);
    assert.equal(old.checked, false);
    s.ids["acknowledge-duplicate"] = old; old.isConnected = true;
    await s.approve();
    assert.equal(s.calls.at(-1).body.acknowledge_duplicate, undefined,
      "reattachment after refusal cannot restore stale confirmation");
    old.checked = true; old.closest = () => null;
    await s.approve();
    assert.equal(s.calls.at(-1).body.acknowledge_duplicate, undefined);
    old.closest = () => s.ids["plan-actions"];
    old.checked = true; s.ids["plan-actions"].isConnected = false;
    await s.approve();
    assert.equal(s.calls.at(-1).body.acknowledge_duplicate, undefined);
    s.ids["plan-actions"].isConnected = true;
    const originalActions = s.ids["plan-actions"];
    old.checked = true; s.ids["plan-actions"] = {...originalActions};
    await s.approve();
    assert.equal(s.calls.at(-1).body.acknowledge_duplicate, undefined);
    s.ids["plan-actions"] = originalActions;
    old.checked = true;
    await s.approve();
    assert.equal(s.calls.at(-1).body.acknowledge_duplicate, true,
      "current original control can be independently checked again");
    await s.approve();
    assert.equal(s.calls.at(-1).body.acknowledge_duplicate, undefined,
      "error retry cannot reuse consumed confirmation");
  }
  s = setup();
  assert.equal(s.ids["acknowledge-duplicate"].checked, false, "new document resets consent");
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    subprocess.run(
        [node, "-e", script],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
