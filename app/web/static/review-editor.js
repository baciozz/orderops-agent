"use strict";
(() => {
  const form = document.getElementById("review-editor");
  if (!form) return;
  const input = document.getElementById("review-changes");
  const actions = document.getElementById("review-actions");
  const template = document.getElementById("review-action-template");
  const error = document.getElementById("review-editor-error");
  const add = document.getElementById("review-add-action");
  const save = document.getElementById("review-save-actions");
  function fail(message) { error.hidden = false; error.textContent = message; }
  function append(values = {}) {
    if (actions.children.length >= 100) {
      fail("每份清单最多100项动作，请拆分任务。");
      return;
    }
    const row = template.content.firstElementChild.cloneNode(true);
    row.querySelectorAll("[data-field]").forEach(field => {
      field.value = values[field.dataset.field] ?? (field.dataset.field === "mode" ? "set_total" : "");
    });
    row.querySelector("[data-remove]").addEventListener("click", () => row.remove());
    actions.append(row);
  }
  try {
    const initial = JSON.parse(input.value);
    if (!Array.isArray(initial)) throw new Error("invalid");
    initial.forEach(append);
    add.disabled = false;
    save.disabled = false;
  } catch (_) {
    fail("动作资料无法载入，请保留当前页并刷新核对，不会提交不完整提案。");
    return;
  }
  add.addEventListener("click", () => append());
  form.addEventListener("submit", event => {
    const rows = Array.from(actions.children).map(row => Object.fromEntries(
      Array.from(row.querySelectorAll("[data-field]")).map(field => [field.dataset.field, field.value])
    ));
    if (!rows.length) {
      event.preventDefault();
      fail("请保留至少一项明确动作；无需变更请返回核对原始需求。");
      return;
    }
    input.value = JSON.stringify(rows);
  });
})();
