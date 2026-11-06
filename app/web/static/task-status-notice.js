"use strict";

// Observation only. Never update the rendered task, forms, or authorization state.
(() => {
  const source = document.getElementById("task-status-baseline");
  const notice = document.getElementById("task-status-notice");
  if (!source || !notice) return;
  // One lifetime per document, including after any stop. Not browser storage or a
  // security boundary: repeated script evaluation must not create another loop.
  const initialized = Symbol.for("order-ops.task-status-notice.initialized");
  if (document[initialized]) return;
  document[initialized] = true;
  const messages = {
    watching: "当前显示为页面加载时的状态；正在限时检查更新，不会自动执行任务。",
    changed: "任务状态有更新，请先保留未提交内容，再刷新查看最新结果。刷新不会自动执行任务。",
    failed: "暂时无法检查更新，检查已停止；当前显示可能已过时，请先保留未提交内容，再手动刷新核对。",
    stopped: "更新检查已停止；当前显示可能已过时，请先保留未提交内容，再手动刷新核对。",
    settled: "当前任务无需自动检查；如需核对最新结果，请先保留未提交内容，再手动刷新。",
  };
  const statuses = ["RECEIVED", "QUEUED", "RUNNING", "NEEDS_CLARIFICATION",
    "AWAITING_APPROVAL", "COMMITTING", "SUCCEEDED", "CANCELLED", "SUPERSEDED",
    "FAILED", "RECONCILING"];
  const terminal = ["SUCCEEDED", "CANCELLED", "SUPERSEDED", "FAILED"];
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const integer = value => Number.isSafeInteger(value) && value >= 0;
  const nullableText = value => value === null || (typeof value === "string" && value.length > 0);
  const date = value => value === null ||
    (typeof value === "string" && Number.isFinite(Date.parse(value)));
  function observation(value) {
    if (!object(value) || !object(value.card) || !object(value.approval) ||
        !object(value.result)) throw new Error("shape");
    const c = value.card, a = value.approval, r = value.result;
    if (typeof c.task_id !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(c.task_id) ||
        !statuses.includes(c.status) || !integer(c.intent_revision) ||
        !["none", "human", "subagent"].includes(c.waiting_on) ||
        !nullableText(a.plan_hash) || !date(a.generated_at) || !date(a.expires_at) ||
        typeof a.approvable !== "boolean" ||
        ![null, "STALE", "EXPIRED", "CONSUMED"].includes(a.disabled_reason) ||
        !nullableText(r.operation_id) || !(r.revision === null || integer(r.revision)) ||
        ![null, "pending", "passed", "partial", "unassessed"].includes(r.verification) ||
        typeof r.verification_manual_required !== "boolean" ||
        !integer(r.verification_retry_attempts) || !date(r.verification_next_attempt_at)) {
      throw new Error("fields");
    }
    // Fixed order; ignore request IDs, sources, and unrelated response fields.
    return JSON.stringify([c.task_id, c.status, c.intent_revision, c.waiting_on,
      a.plan_hash, a.generated_at, a.expires_at, a.approvable, a.disabled_reason,
      r.operation_id, r.revision, r.verification, r.verification_manual_required,
      r.verification_retry_attempts, r.verification_next_attempt_at,
      r.verification_details?.observed_at ?? null, r.verification_details?.status ?? null]);
  }
  let rendered, baseline;
  try {
    rendered = JSON.parse(source.textContent);
    baseline = observation(rendered);
  } catch {
    notice.textContent = messages.failed;
    return;
  }
  const id = rendered.card.task_id;
  if (id === "T-STUB") return;
  if (terminal.includes(rendered.card.status) && rendered.result.verification !== "pending") {
    notice.textContent = messages.settled;
    return;
  }
  const started = performance.now(), wallStarted = Date.now();
  const limit = 300000, maxReads = 60, interval = 5000, timeout = 10000;
  let stopped = false, reads = 0, nextTimer, deadlineTimer, requestTimer, controller;
  function stop(message) {
    if (stopped) return;
    stopped = true; // Invalidates even a late response that ignores AbortSignal.
    clearTimeout(nextTimer);
    clearTimeout(deadlineTimer);
    clearTimeout(requestTimer);
    controller?.abort();
    notice.textContent = message;
  }
  function eligible() {
    if (stopped) return false;
    if (document.hidden || performance.now() - started >= limit || Date.now() - wallStarted >= limit) {
      stop(messages.stopped);
      return false;
    }
    return true;
  }
  function schedule() {
    if (!eligible()) return;
    if (reads >= maxReads) return stop(messages.stopped);
    nextTimer = setTimeout(check, interval);
  }
  async function check() {
    if (!eligible()) return;
    if (reads >= maxReads) return stop(messages.stopped);
    reads++;
    controller = new AbortController();
    requestTimer = setTimeout(() => stop(messages.failed), timeout);
    try {
      const response = await fetch(`/tasks/${encodeURIComponent(id)}/view`, {
        method: "GET", cache: "no-store", signal: controller.signal, redirect: "error",
      });
      if (!eligible()) return;
      if (!response.ok) throw new Error("response");
      const value = await response.json();
      if (!eligible()) return;
      const current = observation(value);
      if (value.card.task_id !== id) throw new Error("identity");
      if (current !== baseline) return stop(messages.changed);
    } catch {
      stop(messages.failed);
      return;
    } finally {
      clearTimeout(requestTimer);
    }
    controller = null;
    schedule(); // Serial: not scheduled until fetch AND body parsing have finished.
  }
  // Conservative one-shot lifetime: no automatic restart after visibility or BFCache.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stop(messages.stopped);
  });
  window.addEventListener("pagehide", () => stop(messages.stopped));
  // Capture before existing handlers start mutations. Do not cancel events or touch controls.
  document.addEventListener("submit", () => stop(messages.stopped), true);
  document.addEventListener("click", event => {
    if (event.target.closest?.("button")) stop(messages.stopped);
  }, true);
  notice.textContent = messages.watching;
  deadlineTimer = setTimeout(() => stop(messages.stopped), limit);
  schedule();
})();
