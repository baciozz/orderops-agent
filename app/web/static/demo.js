"use strict";

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
  const approve = document.getElementById("approve");
  const commit = document.getElementById("commit");
  const message = document.getElementById("action-message");
  const data = actions.dataset;
  let approvalId = data.approvalId;
  const planURL = `/plans/${encodeURIComponent(data.planId)}`;

  approve.addEventListener("click", async () => {
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
      commit.disabled = !approvalId;
      showMessage(message, error.message, true);
    }
  });

  commit.addEventListener("click", async () => {
    if (!approvalId) return;
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
    }
  });
}

document.querySelectorAll("form[data-worker]").forEach((form) => {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
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
      button.disabled = false;
    }
  });
});
