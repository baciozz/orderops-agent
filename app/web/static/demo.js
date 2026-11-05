"use strict";

let existingActionBusy = false;

async function postJSON(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const error = payload?.error;
    throw new Error(`${error?.code || response.status}：${error?.message || "请求失败，请刷新后重试"}`);
  }
  if (!payload) throw new Error("服务端返回格式不正确，请刷新后重试");
  return payload;
}

function showMessage(element, text, failed = false) {
  element.textContent = text;
  element.classList.toggle("error", failed);
  element.setAttribute("role", failed ? "alert" : "status");
}

const actions = document.getElementById("plan-actions");
if (actions) {
  const approve = document.getElementById("renew") || document.getElementById("approve");
  const commit = document.getElementById("commit");
  const message = document.getElementById("action-message");
  const data = actions.dataset;
  let approvalId = data.approvalId;
  const planURL = `/plans/${encodeURIComponent(data.planId)}`;

  approve.addEventListener("click", async () => {
    if (existingActionBusy) return;
    existingActionBusy = true;
    const couldCommit = !commit.disabled;
    approve.disabled = true;
    commit.disabled = true;
    showMessage(message, "正在批准…");
    try {
      const result = await postJSON(`${planURL}/approve`, {
        plan_hash: data.planHash,
        expected_intent_revision: Number(data.intentRevision),
      });
      approvalId = result.approval_id;
      commit.disabled = false;
      showMessage(message, `批准成功（${result.status}），请点击「提交生效」。有效期至 ${result.expires_at}`);
      window.location.reload();
    } catch (error) {
      approve.disabled = false;
      commit.disabled = !couldCommit;
      showMessage(message, error.message, true);
    } finally {
      existingActionBusy = false;
    }
  });

  commit.addEventListener("click", async () => {
    if (!approvalId || existingActionBusy) return;
    existingActionBusy = true;
    const couldApprove = !approve.disabled;
    approve.disabled = true;
    commit.disabled = true;
    showMessage(message, "正在提交生效…");
    try {
      await postJSON(`${planURL}/commit`, {
        approval_id: approvalId,
        plan_hash: data.planHash,
        // Stable across retries and reloads for this approval.
        idempotency_key: `demo-commit-${data.taskId}-${approvalId}`,
      });
      showMessage(message, "提交成功，正在刷新状态与核验结果…");
      window.location.reload();
    } catch (error) {
      approve.disabled = !couldApprove;
      commit.disabled = false;
      showMessage(message, error.message, true);
    } finally {
      existingActionBusy = false;
    }
  });
}

document.querySelectorAll("form[data-worker]").forEach((form) => {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (existingActionBusy) return;
    existingActionBusy = true;
    const button = form.querySelector("button");
    const message = form.querySelector("[data-worker-message]");
    button.disabled = true;
    showMessage(message, "正在运行 FIXTURE worker…");
    try {
      const payload = await postJSON(form.action, {});
      const result = payload.result;
      if (result) {
        showMessage(message, `处理任务 ${result.task_id}：${result.status}${result.error_code ? `（${result.error_code}）` : ""}。`);
        const link = document.createElement("a");
        link.href = `/ui/tasks/${encodeURIComponent(result.task_id)}`;
        link.textContent = "查看处理任务";
        message.append(" ", link);
      } else {
        showMessage(message, `当前无可处理的排队任务；恢复 ${payload.recovered_task_ids.length} 条，投递 ${payload.published} 条通知。`);
      }
      if (actions) window.location.reload();
    } catch (error) {
      showMessage(message, error.message, true);
    } finally {
      existingActionBusy = false;
      button.disabled = false;
    }
  });
});

// All mutations on a task page share a lock. Server guards remain authoritative.
const taskInputs = document.getElementById("task-inputs");
if (taskInputs) {
  const taskId = taskInputs.dataset.taskId;
  const taskURL = `/tasks/${encodeURIComponent(taskId)}`;
  const generalMessage = document.getElementById("task-input-message");
  let busy = false;

  async function mutate(message, operation) {
    if (busy || existingActionBusy) {
      showMessage(message, "另一个操作正在进行，请等待后刷新核对。", true);
      return;
    }
    busy = true;
    const controls = [...document.querySelectorAll("button, input, textarea")];
    const disabled = controls.map((control) => control.disabled);
    controls.forEach((control) => { control.disabled = true; });
    try {
      await operation();
    } catch (error) {
      showMessage(message, `${error.message}。若结果不确定，请先刷新核对，勿盲目重复上传。`, true);
    } finally {
      controls.forEach((control, index) => { control.disabled = disabled[index]; });
      busy = false;
    }
  }

  async function checkCurrent() {
    const response = await fetch(`${taskURL}/view`, { cache: "no-store" });
    if (!response.ok) throw new Error("无法核对最新任务，请刷新");
    const current = (await response.json()).card;
    if (current.intent_revision !== Number(taskInputs.dataset.intentRevision) ||
        current.status !== taskInputs.dataset.status) {
      throw new Error("任务已变化，请刷新后重新核对");
    }
  }

  const upload = document.getElementById("source-upload");
  const evidenceFile = document.getElementById("evidence-file");
  evidenceFile?.addEventListener("change", () => {
    document.querySelectorAll('input[name="answer-source"]').forEach((input) => {
      input.checked = false;
    });
    showMessage(document.getElementById("upload-message"),
      "文件选择已改变，旧证据已取消勾选。请先上传或清空文件选择，再提交回答。");
  });
  upload?.addEventListener("submit", (event) => {
    event.preventDefault();
    const file = document.getElementById("evidence-file").files[0];
    const message = document.getElementById("upload-message");
    if (!file) return;
    if (file.size > 5 * 1024 * 1024 || !/\.(txt|md|xlsx)$/i.test(file.name)) {
      showMessage(message, "请选择不超过 5 MiB 的 .txt、.md 或 .xlsx 文件", true);
      return;
    }
    mutate(message, async () => {
      await checkCurrent();
      showMessage(message, "正在上传并解析…");
      const body = new FormData();
      body.append("task_id", taskId);
      body.append("file", file);
      const response = await fetch("/sources", { method: "POST", body });
      const payload = await response.json().catch(() => null);
      if (!response.ok || payload?.parser_status !== "ok") {
        throw new Error(`${payload?.error?.code || response.status}：${payload?.error?.message || "上传结果未知，请刷新核对文件列表"}`);
      }
      const list = document.getElementById("uploaded-sources");
      list.querySelector("[data-empty]")?.remove();
      // Do not carry forward selections after changing the evidence set.
      list.querySelectorAll("input").forEach((input) => { input.checked = false; });
      const item = document.createElement("li");
      const label = document.createElement("label");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.name = "answer-source";
      checkbox.value = payload.source_id;
      label.append(checkbox, ` ${file.name}（${file.size} 字节） · ${payload.source_id}`);
      item.append(label);
      list.append(item);
      upload.reset();
      showMessage(message, "上传成功（parser_status: ok）。请重新勾选本次回答需要的证据；尚未入队。");
    });
  });

  document.getElementById("clarification-answer")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = document.getElementById("answer-text").value.trim();
    const sourceIds = [...document.querySelectorAll('input[name="answer-source"]:checked')]
      .map((input) => input.value).sort();
    const message = document.getElementById("answer-message");
    if (evidenceFile?.files.length) {
      showMessage(message, "所选文件尚未成功上传。请先上传或清空文件选择，再提交回答。", true);
      return;
    }
    if (!text && !sourceIds.length) {
      showMessage(message, "请输入回答或勾选至少一个证据文件", true);
      return;
    }
    mutate(message, async () => {
      await checkCurrent();
      // Same revision and exact payload have the same receipt key across retries/reloads.
      // Changing either text or evidence creates a distinct delivery identity.
      const bytes = new TextEncoder().encode(JSON.stringify([
        taskId, taskInputs.dataset.intentRevision, text, sourceIds,
      ]));
      const digest = await crypto.subtle.digest("SHA-256", bytes);
      const key = [...new Uint8Array(digest)].map((n) => n.toString(16).padStart(2, "0")).join("");
      showMessage(message, "正在提交回答…");
      await postJSON(`${taskURL}/answers`, {
        thread_id: `demo-answer-${taskId}`,
        client_event_key: key,
        text,
        source_ids: sourceIds,
      });
      showMessage(message, "回答已入队。刷新后请另行运行 FIXTURE worker。");
      window.location.reload();
    });
  });

  document.getElementById("cancel-task")?.addEventListener("click", () => {
    if (busy || !window.confirm("确定取消此任务？此操作不会撤销已提交的订单变更。")) return;
    mutate(generalMessage, async () => {
      await checkCurrent();
      const result = await postJSON(`${taskURL}/cancel`, {
        expected_intent_revision: Number(taskInputs.dataset.intentRevision),
      });
      if (result.status === "ALREADY_COMMITTED") {
        showMessage(generalMessage, "任务已进入提交阶段，未取消。请刷新查看结果。", true);
        return;
      }
      showMessage(generalMessage, "任务已取消，正在刷新…");
      window.location.reload();
    });
  });

  // Prevent approval/commit/worker from racing an upload or answer in this page.
  document.addEventListener("click", (event) => {
    if (busy && event.target.closest("button")) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }, true);
}

// Browsers can restore checkbox state or a stale task page through Back/Forward.
window.addEventListener("pageshow", (event) => {
  document.querySelectorAll('input[name="answer-source"]').forEach((input) => {
    input.checked = false;
  });
  if (event.persisted && taskInputs) window.location.reload();
});
