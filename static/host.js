"use strict";

const token = location.pathname.split("/").filter(Boolean)[1];
const errorEl = document.getElementById("error");
const publishBtn = document.getElementById("publish");

async function refresh() {
  let s;
  try {
    const r = await fetch("/api/h/" + token);
    const data = await r.json();
    if (!r.ok) {
      document.getElementById("content").hidden = true;
      const fatal = document.getElementById("fatal");
      fatal.hidden = false;
      fatal.textContent = data.error || "主持链接无效";
      return;
    }
    s = data;
  } catch {
    return; // 网络抖动，下次轮询再试
  }
  render(s);
}

function render(s) {
  document.getElementById("published-banner").hidden = !s.published;
  document.getElementById("closed-banner").hidden = !s.closed;
  // 封存区仅公布后、未封存时出现；封存后一切主持写操作按钮禁用
  document.getElementById("close-section").hidden = !s.published || !!s.closed;
  publishBtn.disabled = !s.can_publish || !!s.closed;

  const pending = s.reviewers.filter((r) => !r.submitted).map((r) => r.name);
  const acked = s.reviewers.filter((r) => r.acknowledged).length;
  document.getElementById("pending-summary").textContent = s.published
    ? "互评已结束。已读回执：" + acked + "/" + s.reviewers.length + " 已确认。"
    : pending.length
      ? "待交人员：" + pending.join("、")
      : "全部已交，可以公布。";

  // 已读回执列仅公布后展示（按留存名单原顺序），不含意见或问答内容
  document.getElementById("ack-head").hidden = !s.published;
  document.getElementById("ack-note").hidden = !s.published;

  const tbody = document.getElementById("reviewers");
  tbody.innerHTML = "";
  for (const r of s.reviewers) {
    const tr = document.createElement("tr");
    const tdName = document.createElement("td");
    tdName.textContent = r.name;
    const tdStatus = document.createElement("td");
    tdStatus.textContent = r.submitted ? "已交" : "待交";
    tdStatus.className = r.submitted ? "ok" : "pending";
    tr.append(tdName, tdStatus);
    if (s.published) {
      const tdAck = document.createElement("td");
      tdAck.textContent = r.acknowledged
        ? "已确认（" + r.acknowledged_at + "）"
        : "待确认";
      tdAck.className = r.acknowledged ? "ok" : "pending";
      tr.appendChild(tdAck);
    }
    tbody.appendChild(tr);
  }

  renderReports(s.published ? (s.reports || []) : []);
  renderAnthology(s);

  const linksBody = document.getElementById("links");
  linksBody.innerHTML = "";
  for (const l of s.links) {
    const tr = document.createElement("tr");
    const tdName = document.createElement("td");
    tdName.textContent = l.name;
    const tdLink = document.createElement("td");
    const a = document.createElement("a");
    a.href = location.origin + l.url;
    a.textContent = location.origin + l.url;
    tdLink.appendChild(a);
    const tdAction = document.createElement("td");
    const rotateBtn = document.createElement("button");
    rotateBtn.type = "button";
    rotateBtn.className = "link";
    rotateBtn.textContent = "换发链接";
    rotateBtn.dataset.name = l.name;
    rotateBtn.dataset.link = location.origin + l.url;
    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "link-danger remove-member";
    removeBtn.textContent = "移除成员";
    removeBtn.dataset.name = l.name;
    // 公布后不可移除：按钮禁用；封存后换发同样停用
    rotateBtn.disabled = !!s.closed;
    removeBtn.disabled = !!s.published;
    tdAction.append(rotateBtn, removeBtn);
    tr.append(tdName, tdLink, tdAction);
    linksBody.appendChild(tr);
  }
}

function renderReports(reports) {
  document.getElementById("reports-section").hidden = !reports.length;
  const statusText = {
    pending: "待处理",
    kept: "已保留",
    blocked: "已屏蔽",
  };
  const tbody = document.getElementById("reports");
  tbody.innerHTML = "";
  for (const r of reports) {
    const tr = document.createElement("tr");

    const tdId = document.createElement("td");
    tdId.textContent = "#" + r.id;

    const tdReason = document.createElement("td");
    tdReason.textContent = r.reason;

    const tdReview = document.createElement("td");
    tdReview.textContent = r.review;

    const tdStatus = document.createElement("td");
    tdStatus.textContent = statusText[r.status] || r.status;
    tdStatus.className = r.status === "pending" ? "pending" : "ok";

    const tdAction = document.createElement("td");
    if (r.status === "pending") {
      const keepBtn = document.createElement("button");
      keepBtn.type = "button";
      keepBtn.className = "link";
      keepBtn.textContent = "保留";
      keepBtn.dataset.id = r.id;
      keepBtn.dataset.decision = "keep";

      const blockBtn = document.createElement("button");
      blockBtn.type = "button";
      blockBtn.className = "link-danger";
      blockBtn.textContent = "屏蔽";
      blockBtn.dataset.id = r.id;
      blockBtn.dataset.decision = "block";

      tdAction.append(keepBtn, blockBtn);
    } else {
      tdAction.textContent = "—";
    }

    tr.append(tdId, tdReason, tdReview, tdStatus, tdAction);
    tbody.appendChild(tr);
  }
}

// 匿名合辑：公布后显示各人授权，勾选姓名发布一次；发布后只显示公开链接
function renderAnthology(s) {
  const section = document.getElementById("anthology-section");
  section.hidden = !s.published;
  if (!s.published) return;
  const form = document.getElementById("anthology-form");
  const done = document.getElementById("anthology-done");
  if (s.anthology_url) {
    // 已发布：只展示公开链接（封存后仍可只读访问），不再显示选择表单
    form.hidden = true;
    done.hidden = false;
    const a = document.getElementById("anthology-link");
    a.href = location.origin + s.anthology_url;
    a.textContent = location.origin + s.anthology_url;
    return;
  }
  form.hidden = false;
  done.hidden = true;
  const tbody = document.getElementById("anthology-members");
  tbody.innerHTML = "";
  for (const r of s.reviewers) {
    const tr = document.createElement("tr");
    const tdPick = document.createElement("td");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = r.name;
    cb.disabled = !!s.closed;
    tdPick.appendChild(cb);
    const tdName = document.createElement("td");
    tdName.textContent = r.name;
    const tdConsent = document.createElement("td");
    tdConsent.textContent = r.anthology_consent ? "已授权" : "未授权";
    tdConsent.className = r.anthology_consent ? "ok" : "pending";
    tr.append(tdPick, tdName, tdConsent);
    tbody.appendChild(tr);
  }
  document.getElementById("anthology-publish").disabled = !!s.closed;
}

// 发布匿名合辑：按勾选的姓名一次发布；任一入选稿未授权则整次失败
document.getElementById("anthology-publish").addEventListener("click", async () => {
  const errorEl = document.getElementById("anthology-error");
  errorEl.textContent = "";
  const names = [
    ...document.querySelectorAll("#anthology-members input[type=checkbox]:checked"),
  ].map((cb) => cb.value);
  if (!names.length) {
    errorEl.textContent = "请至少选择一名成员。";
    return;
  }
  if (
    !confirm(
      "确定发布匿名合辑？\n" +
        "发布为一次性操作：入选稿件按原名单顺序编号固化，公开页只显示编号与稿文；\n" +
        "发布后授权冻结，不能重复发布。"
    )
  )
    return;
  const btn = document.getElementById("anthology-publish");
  btn.disabled = true;
  try {
    const r = await fetch("/api/h/" + token + "/anthology", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ names }),
    });
    const data = await r.json();
    if (!r.ok) {
      errorEl.textContent =
        (data.error || "发布失败") +
        (data.missing_consent ? "（未授权：" + data.missing_consent.join("、") + "）" : "");
      btn.disabled = false;
      return;
    }
  } catch {
    errorEl.textContent = "网络错误，请重试";
    btn.disabled = false;
    return;
  }
  refresh();
});

publishBtn.addEventListener("click", async () => {
  if (!confirm("确定公布？公布后稿件与意见均不可再修改。")) return;
  errorEl.textContent = "";
  try {
    const r = await fetch("/api/h/" + token + "/publish", { method: "POST" });
    const data = await r.json();
    if (!r.ok) {
      errorEl.textContent =
        (data.error || "公布失败") +
        (data.missing ? "（缺：" + data.missing.join("、") + "）" : "");
    }
  } catch {
    errorEl.textContent = "网络错误，请重试";
  }
  refresh();
});

// 永久封存：仅公布后、无未答澄清且无待处理举报时可成功；封存后双方只读
document.getElementById("close-session").addEventListener("click", async () => {
  const closeError = document.getElementById("close-error");
  closeError.textContent = "";
  if (
    !confirm(
      "确定永久封存本会话？\n" +
        "封存后双方只能只读查阅：提问、答复、确认已读、举报、换发链接等一切写入都会被拒绝；\n" +
        "封存不可撤销，但不会删除任何数据或链接，匿名与现有阅读权限保持不变。"
    )
  )
    return;
  const btn = document.getElementById("close-session");
  btn.disabled = true;
  try {
    const r = await fetch("/api/h/" + token + "/close", { method: "POST" });
    const data = await r.json();
    if (!r.ok) {
      closeError.textContent = data.error || "封存失败，请刷新主持台后重试";
      btn.disabled = false;
      return;
    }
  } catch {
    closeError.textContent = "网络错误，请重试";
    btn.disabled = false;
    return;
  }
  refresh();
});

// 换发某人的个人链接：请求携带姓名与页面所见链接，成功后原链接立即作废
document.getElementById("links").addEventListener("click", async (e) => {
  const btn = e.target.closest("button.link");
  if (!btn) return;
  const name = btn.dataset.name;
  const link = btn.dataset.link;
  const rotateError = document.getElementById("rotate-error");
  rotateError.textContent = "";
  if (
    !confirm(
      `确定为「${name}」换发链接？\n` +
        "原链接将立即作废，之后再使用原链接读取或提交一律无效；" +
        "新链接仍沿用其稿件、版本、分配、意见与问答，其他人不受影响。"
    )
  )
    return;
  btn.disabled = true;
  try {
    const r = await fetch("/api/h/" + token + "/rotate-link", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, link }),
    });
    const data = await r.json();
    if (!r.ok) {
      rotateError.textContent =
        "换发「" + name + "」失败：" + (data.error || "请刷新主持台后重试");
      return;
    }
    alert(
      "「" + name + "」的新个人链接：\n" + location.origin + data.url +
      "\n原链接已作废，请尽快把新链接分发给本人。"
    );
  } catch {
    rotateError.textContent = "网络错误，请重试";
  }
  refresh();
});

// 公布前移除一名退出互评的成员：名单、分配与意见一起生效，不支持补人或恢复
document.getElementById("links").addEventListener("click", async (e) => {
  const btn = e.target.closest("button.remove-member");
  if (!btn) return;
  const name = btn.dataset.name;
  const removeError = document.getElementById("remove-error");
  removeError.textContent = "";
  if (
    !confirm(
      `确定移除「${name}」？\n` +
        "移除后名单立即按原录入顺序重新闭合互评环（至少保留 3 人），不能补人也不能恢复：\n" +
        "· 「" + name + "」的个人链接永久失效，其发出或收到的意见不计入公布条件；\n" +
        "· 只有原来评「" + name + "」的人会改评下一位留存成员，其旧意见失效、需重新阅读提交；\n" +
        "· 其他人的稿件、版本、意见与链接全部保留。"
    )
  )
    return;
  btn.disabled = true;
  try {
    const r = await fetch("/api/h/" + token + "/remove", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const data = await r.json();
    if (!r.ok) {
      removeError.textContent =
        "移除「" + name + "」失败：" + (data.error || "请刷新主持台后重试");
    }
  } catch {
    removeError.textContent = "网络错误，请重试";
  }
  refresh();
});

// 主持人处理举报：裁定只提交一次；成功后轮询刷新，重复处理由服务端返回 409
document.getElementById("reports").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-decision]");
  if (!btn) return;
  const id = btn.dataset.id;
  const decision = btn.dataset.decision;
  const actionText = decision === "keep" ? "保留" : "屏蔽";
  const errorEl = document.getElementById("report-decision-error");
  errorEl.textContent = "";
  if (!confirm(`确定对举报 #${id} 选择「${actionText}」？处理后不可修改。`)) return;
  btn.disabled = true;
  try {
    const r = await fetch(`/api/h/${token}/reports/${id}/${decision}`, { method: "POST" });
    const data = await r.json();
    if (!r.ok) {
      errorEl.textContent = `举报 #${id} 处理失败：` + (data.error || "请刷新主持台后重试");
      btn.disabled = false;
      return;
    }
  } catch {
    errorEl.textContent = "网络错误，请重试";
    btn.disabled = false;
    return;
  }
  refresh();
});

refresh();
setInterval(refresh, 3000);
