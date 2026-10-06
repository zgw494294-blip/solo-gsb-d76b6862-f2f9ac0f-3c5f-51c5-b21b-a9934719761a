"use strict";

// 匿名合辑公开只读页：只按编号展示稿文；内容在发布时固化，无需轮询
const token = location.pathname.split("/").filter(Boolean)[1];

async function load() {
  let data;
  try {
    const r = await fetch("/api/a/" + token);
    data = await r.json();
    if (!r.ok) {
      showFatal(data.error || "公开链接无效");
      return;
    }
  } catch {
    showFatal("网络错误，请刷新重试");
    return;
  }
  const items = document.getElementById("items");
  items.innerHTML = "";
  for (const it of data.items) {
    const section = document.createElement("section");
    section.className = "card";
    const h = document.createElement("h2");
    h.textContent = "第 " + it.number + " 篇";
    const pre = document.createElement("pre");
    pre.className = "manuscript-view";
    pre.textContent = it.manuscript;
    section.append(h, pre);
    items.appendChild(section);
  }
}

function showFatal(text) {
  document.getElementById("content").hidden = true;
  const fatal = document.getElementById("fatal");
  fatal.hidden = false;
  fatal.textContent = text;
}

load();
