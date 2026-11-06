// Deterministic minimal DOM/timers. No sleeps, browser, server, or real fetch.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/web/static/task-status-notice.js', 'utf8');
const clone = value => JSON.parse(JSON.stringify(value));
const base = {
  card: {task_id: 'T-SYNTHETIC', status: 'QUEUED', intent_revision: 0, waiting_on: 'subagent'},
  approval: {plan_hash: null, generated_at: null, expires_at: null, approvable: false, disabled_reason: null},
  result: {operation_id: null, revision: null, verification: null, verification_manual_required: false,
    verification_retry_attempts: 0, verification_next_attempt_at: null},
};
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
function harness({baseline = base, missing = false, hidden = false, malformed = false} = {}) {
  let now = 0, wallOffset = 0, sequence = 0;
  const timers = new Map(), listeners = {}, calls = [];
  const notice = {textContent: 'server fallback'};
  const baselineNode = {textContent: malformed ? '<bad>' : JSON.stringify(baseline)};
  const untouched = {answer: 'unsent text', files: [{name: 'unsent.xlsx'}], evidence: true,
    duplicate: true, buttonDisabled: false};
  const add = prefix => (name, fn, capture) => {
    (listeners[prefix + name] ??= []).push({fn, capture});
  };
  const document = {
    hidden,
    getElementById: id => missing ? null : id === 'task-status-baseline' ? baselineNode :
      id === 'task-status-notice' ? notice : (() => {throw Error('Other DOM access');})(),
    addEventListener: add('doc:'),
  };
  const context = {
    document, window: {addEventListener: add('win:')}, performance: {now: () => now}, Date: class extends Date { static now() { return now + wallOffset; } },
    AbortController, setTimeout: (fn, delay) => {
      const id = ++sequence; timers.set(id, {fn, at: now + delay, delay}); return id;
    }, clearTimeout: id => timers.delete(id),
    fetch: (url, options) => new Promise((resolve, reject) => calls.push({url, options, resolve, reject})),
  };
  const realm = vm.createContext(context);
  const init = () => vm.runInContext(source, realm);
  init();
  const emit = (name, event = {}) => (listeners[name] || []).forEach(({fn}) => fn(event));
  const tick = async (amount) => {
    const end = now + amount;
    while (true) {
      const next = [...timers].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
      if (!next) break;
      now = next[1].at; timers.delete(next[0]); next[1].fn(); await flush();
    }
    now = end; await flush();
  };
  const finish = async (value = baseline, index = calls.length - 1) => {
    calls[index].resolve({ok: true, json: async () => clone(value)}); await flush();
  };
  const checkPreserved = () => assert.deepEqual(untouched, {answer: 'unsent text',
    files: [{name: 'unsent.xlsx'}], evidence: true, duplicate: true, buttonDisabled: false});
  return {init, notice, timers, calls, document, emit, tick, finish, checkPreserved, listeners,
    // Independently exercise read cap even if the deadline callback is delayed.
    firePoll: async () => {
      const next = [...timers].find(([, t]) => t.delay === 5000); assert.ok(next);
      timers.delete(next[0]); next[1].fn(); await flush();
    },
    jump: value => {now = value;}, wallOffset: value => {wallOffset = value;},
  };
}
async function changed(mutate) {
  const h = harness(); const latest = clone(base); mutate(latest);
  // Change occurs before the first read, not after learning a fetch baseline.
  await h.tick(5000); await h.finish(latest);
  assert.match(h.notice.textContent, /有更新/); assert.equal(h.timers.size, 0);
  assert.equal(h.calls[0].url, '/tasks/T-SYNTHETIC/view');
  assert.equal(h.calls[0].options.method, 'GET');
  assert.equal(h.calls[0].options.cache, 'no-store');
  assert.equal(h.calls[0].options.redirect, 'error');
  await h.tick(600000); assert.equal(h.calls.length, 1); h.checkPreserved();
}
(async () => {
  for (const mutate of [
    v => v.card.status = 'RUNNING', v => v.card.intent_revision++, v => v.card.waiting_on = 'human',
    v => v.approval.plan_hash = 'new-hash', v => v.approval.generated_at = '2026-11-06T12:00:00Z',
    v => v.approval.expires_at = '2026-11-06T13:00:00Z', v => v.approval.approvable = true,
    v => v.approval.disabled_reason = 'EXPIRED', v => v.result.operation_id = 'OP-SYNTHETIC',
    v => v.result.revision = 1, v => v.result.verification = 'pending',
    v => v.result.verification_manual_required = true, v => v.result.verification_retry_attempts++,
    v => v.result.verification_next_attempt_at = '2026-11-06T14:00:00Z',
  ]) await changed(mutate);
  const pending = clone(base); pending.card.status = 'SUCCEEDED'; pending.result.verification = 'pending';
  for (const mutate of [v => v.result.verification = 'passed', v => v.result.verification_manual_required = true]) {
    const h = harness({baseline: pending}), latest = clone(pending); mutate(latest);
    await h.tick(5000); await h.finish(latest); assert.match(h.notice.textContent, /有更新/);
  }
  for (const status of ['SUCCEEDED', 'FAILED', 'CANCELLED', 'SUPERSEDED']) {
    const settled = clone(base); settled.card.status = status;
    const h = harness({baseline: settled}); await h.tick(600000);
    assert.equal(h.calls.length, 0); assert.match(h.notice.textContent, /无需自动检查/);
  }
  for (const options of [{missing: true}, {baseline: {...base, card: {...base.card, task_id: 'T-STUB'}}}]) {
    const h = harness(options); assert.equal(h.timers.size, 0); assert.equal(h.calls.length, 0);
  }
  for (const options of [{malformed: true}, {baseline: {}}, {baseline: {...base, card: {...base.card, task_id: '../other'}}}]) {
    const h = harness(options); assert.match(h.notice.textContent, /无法检查/); assert.equal(h.timers.size, 0);
  }
  { // Ignore volatile fields; serialize slow responses including JSON parsing.
    const h = harness(); await h.tick(5000); await h.tick(8000); assert.equal(h.calls.length, 1);
    let body; h.calls[0].resolve({ok: true, json: () => new Promise(resolve => {body = resolve;})});
    await flush(); await h.tick(1000); assert.equal(h.calls.length, 1);
    body({...clone(base), request_id: 'noise', correlation_id: 'noise'}); await flush();
    assert.match(h.notice.textContent, /限时检查/); await h.tick(4999); assert.equal(h.calls.length, 1);
    await h.tick(1); assert.equal(h.calls.length, 2); await h.finish(); h.checkPreserved();
  }
  for (const invalid of [null, {}, [], {...base, card: {...base.card, task_id: 'OTHER'}},
    {...base, approval: {...base.approval, expires_at: 'secret-error'}},
    {...base, result: {...base.result, verification_retry_attempts: '0'}},
    {...base, card: {...base.card, status: 'secret-error'}}]) {
    const h = harness(); await h.tick(5000); await h.finish(invalid);
    assert.match(h.notice.textContent, /无法检查/); assert.doesNotMatch(h.notice.textContent, /secret-error/);
    assert.equal(h.timers.size, 0); h.checkPreserved();
  }
  for (const failure of ['network', 'http', 'json', 'timeout', 'body-timeout']) {
    const h = harness(); await h.tick(5000);
    if (failure === 'network') h.calls[0].reject(Error('secret-error'));
    if (failure === 'http') h.calls[0].resolve({ok: false});
    if (failure === 'json') h.calls[0].resolve({ok: true, json: async () => {throw Error('secret-error');}});
    if (failure === 'body-timeout') h.calls[0].resolve({ok: true, json: () => new Promise(() => {})});
    await flush(); if (failure.endsWith('timeout')) await h.tick(10000);
    assert.match(h.notice.textContent, /无法检查/); assert.doesNotMatch(h.notice.textContent, /secret-error/);
    const message = h.notice.textContent; await h.finish({...base, card: {...base.card, status: 'RUNNING'}});
    assert.equal(h.notice.textContent, message); assert.equal(h.timers.size, 0);
    assert.equal(h.calls[0].options.signal.aborted, true); h.checkPreserved();
  }
  for (const lifecycle of ['hidden', 'pagehide', 'button', 'submit']) {
    const h = harness(); await h.tick(5000);
    if (lifecycle === 'hidden') { h.document.hidden = true; h.emit('doc:visibilitychange'); }
    if (lifecycle === 'pagehide') h.emit('win:pagehide', {persisted: true});
    if (lifecycle === 'button') h.emit('doc:click', {target: {closest: selector => selector === 'button' ? {} : null}});
    if (lifecycle === 'submit') h.emit('doc:submit');
    const message = h.notice.textContent; assert.match(message, /已停止/);
    assert.equal(h.calls[0].options.signal.aborted, true);
    await h.finish({...base, card: {...base.card, status: 'RUNNING'}});
    h.document.hidden = false; h.emit('doc:visibilitychange');
    for (let i = 0; i < 10; i++) h.emit('win:pageshow', {persisted: true});
    await h.tick(600000); assert.equal(h.notice.textContent, message); assert.equal(h.calls.length, 1);
    assert.equal(h.timers.size, 0); h.checkPreserved();
    assert.equal(h.listeners['doc:submit'][0].capture, true);
    assert.equal(h.listeners['doc:click'][0].capture, true);
  }
  { const h = harness({hidden: true}); await h.tick(600000); assert.equal(h.calls.length, 0); }
  { const h = harness(); h.emit('doc:click', {target: {closest: () => null}});
    await h.tick(5000); assert.equal(h.calls.length, 1); }
  { // Independent count cap, without advancing monotonic time or firing deadline.
    const h = harness();
    for (let i = 0; i < 60; i++) { await h.firePoll(); await h.finish(); }
    assert.equal(h.calls.length, 60); assert.equal(h.timers.size, 0); assert.match(h.notice.textContent, /已停止/);
  }
  { // Hard deadline aborts an outstanding read even with no further response.
    const h = harness();
    for (let i = 0; i < 58; i++) { await h.tick(5000); await h.finish(); }
    await h.tick(5000); assert.equal(h.calls.length, 59); await h.tick(5000);
    assert.match(h.notice.textContent, /已停止/); assert.equal(h.calls.at(-1).options.signal.aborted, true);
    const message = h.notice.textContent; await h.finish({...base, card: {...base.card, status: 'RUNNING'}});
    assert.equal(h.notice.textContent, message); assert.equal(h.timers.size, 0);
  }
  { // Delayed deadline timer cannot admit a read or completion past elapsed limit.
    const h = harness(); h.jump(300001); await h.firePoll(); assert.equal(h.calls.length, 0);
    const late = harness(); await late.tick(5000); late.jump(300001); await late.finish();
    assert.match(late.notice.textContent, /已停止/); assert.equal(late.timers.size, 0);
  }
  { // Wall-clock sleep and clock rollback cannot extend the monotonic deadline.
    const sleep = harness(); await sleep.tick(5000); sleep.wallOffset(300000); await sleep.finish();
    assert.match(sleep.notice.textContent, /已停止/); assert.equal(sleep.timers.size, 0);
    const rollback = harness(); await rollback.tick(5000); rollback.wallOffset(-600000);
    rollback.jump(300001); await rollback.finish(); assert.match(rollback.notice.textContent, /已停止/);
  }
  { // Repeated initialization neither duplicates active loops nor renews their budgets.
    const h = harness(); const text = h.notice.textContent;
    for (let i = 0; i < 10; i++) h.init();
    assert.equal(h.timers.size, 2); assert.equal(h.notice.textContent, text);
    assert.equal(h.listeners['doc:submit'].length, 1);
    await h.tick(5000); h.init(); h.init(); assert.equal(h.calls.length, 1);
    await h.finish(); assert.equal(h.timers.size, 2);
    await h.tick(5000); assert.equal(h.calls.length, 2);
    h.emit('doc:submit');
    const stopped = h.notice.textContent; h.init(); await h.finish();
    assert.equal(h.notice.textContent, stopped); assert.equal(h.timers.size, 0);
    await h.tick(600000); assert.equal(h.calls.length, 2);
    // A new document gets its own marker and bounded observation lifetime.
    const fresh = harness(); await fresh.tick(5000); assert.equal(fresh.calls.length, 1);
  }
  for (const stop of ['action', 'hidden', 'pagehide', 'timeout', 'changed', 'count', 'elapsed']) {
    const h = harness();
    if (stop === 'count') {
      for (let i = 0; i < 60; i++) { await h.firePoll(); await h.finish(); }
    } else {
      await h.tick(5000);
      if (stop === 'action') h.emit('doc:submit');
      if (stop === 'hidden') { h.document.hidden = true; h.emit('doc:visibilitychange'); }
      if (stop === 'pagehide') h.emit('win:pagehide');
      if (stop === 'timeout') await h.tick(10000);
      if (stop === 'changed') await h.finish({...base, card: {...base.card, status: 'RUNNING'}});
      if (stop === 'elapsed') { h.jump(300001); await h.finish(); }
    }
    const text = h.notice.textContent, count = h.calls.length;
    assert.equal(h.timers.size, 0);
    h.document.hidden = false;
    for (let i = 0; i < 10; i++) h.init();
    h.emit('win:pageshow', {persisted: true});
    await h.finish({...base, card: {...base.card, status: 'RUNNING'}}, 0);
    await h.tick(600000);
    assert.equal(h.notice.textContent, text); assert.equal(h.calls.length, count);
    assert.equal(h.timers.size, 0); h.checkPreserved();
  }
  console.log('PASS: deterministic baseline, validation, lifecycle, timeout, serial and hard-bound checks');
})().catch(error => { console.error(error); process.exitCode = 1; });
