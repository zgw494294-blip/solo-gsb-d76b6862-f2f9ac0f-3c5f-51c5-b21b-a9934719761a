// 主持页匿名合辑的前端回归自测：修复"轮询丢选"。
//
// 不依赖浏览器与第三方包：用极简 DOM 桩加载真实的 static/host.js（vm 沙箱），
// fetch/confirm/setInterval 全部可控，refresh() 即一次 3 秒轮询。
//
// 运行：node selftest_ui.js
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const HOST_JS = fs.readFileSync(path.join(__dirname, "static", "host.js"), "utf8");
const HOST_TOKEN = "hosttoken";
const NAMES = ["甲", "乙", "丙"];

// ---------------- 极简 DOM 桩（仅实现 host.js 用到的能力） ----------------

class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parent = null;
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.type = "";
    this.value = "";
    this.href = "#";
    this.className = "";
    this.dataset = {};
    this.listeners = Object.create(null);
    this._text = "";
    this._html = "";
  }

  append(...nodes) {
    for (const n of nodes) this.appendChild(n);
  }

  appendChild(n) {
    if (n.parent) {
      const i = n.parent.children.indexOf(n);
      if (i >= 0) n.parent.children.splice(i, 1);
    }
    n.parent = this;
    this.children.push(n);
    return n;
  }

  addEventListener(type, fn) {
    (this.listeners[type] ||= []).push(fn);
  }

  // 与浏览器一致：禁用控件不接收 click/change（用户无法操作）
  dispatchEvent(ev) {
    if (this.disabled) return true;
    ev.target ||= this;
    for (const fn of this.listeners[ev.type] || []) fn(ev);
    return true;
  }

  set textContent(v) {
    this._text = String(v);
    for (const c of this.children) c.parent = null;
    this.children = [];
  }

  get textContent() {
    if (this.children.length) {
      return this.children.map((c) => c.textContent).join("");
    }
    return this._text;
  }

  set innerHTML(v) {
    this._html = String(v);
    if (v === "") {
      for (const c of this.children) c.parent = null;
      this.children = [];
    }
  }

  get innerHTML() {
    return this._html;
  }

  matches(token) {
    return matchesSimple(this, token);
  }

  closest(sel) {
    let el = this;
    while (el) {
      if (matchesSimple(el, sel)) return el;
      el = el.parent;
    }
    return null;
  }
}

function parseSimple(token) {
  const m = token.match(/^([a-z]*)(.*)$/i);
  const tag = m[1] ? m[1].toUpperCase() : null;
  const attrs = [...m[2].matchAll(/\[([a-z-]+)(?:=([^\]]*))?\]/gi)].map(
    (x) => [x[1], x[2]]
  );
  const checked = /:checked\b/.test(m[2]);
  const disabled = /:disabled\b/.test(m[2]);
  return { tag, attrs, checked, disabled };
}

function matchesSimple(el, selector) {
  // 支持 "tag[attr=value]:checked" 形式；#id 前缀在 querySelectorAll 里单独处理
  if (selector.startsWith("#")) return false;
  const want = parseSimple(selector);
  if (want.tag && el.tagName !== want.tag) return false;
  for (const [attr, val] of want.attrs) {
    if (!(attr in el)) return false;
    if (val !== undefined && String(el[attr]) !== val) return false;
  }
  if (want.checked && !el.checked) return false;
  if (want.disabled && !el.disabled) return false;
  return true;
}

function collect(root, out) {
  for (const c of root.children) {
    out.push(c);
    collect(c, out);
  }
}

function makeDocument() {
  const byId = new Map();
  const TAGS = {
    publish: "button",
    "close-session": "button",
    "anthology-publish": "button",
    "anthology-link": "a",
    reviewers: "tbody",
    reports: "tbody",
    "anthology-members": "tbody",
    links: "tbody",
    "ack-head": "th",
  };
  const document = {
    createElement: (tag) => new El(tag),
    getElementById(id) {
      if (!byId.has(id)) byId.set(id, new El(TAGS[id] || "div"));
      return byId.get(id);
    },
    querySelectorAll(selector) {
      const idMatch = selector.match(/#([\w-]+)/);
      const root = idMatch ? this.getElementById(idMatch[1]) : null;
      const simple = selector
        .replace(/#[\w-]+/g, "")
        .trim()
        .split(/\s+/)
        .pop();
      const all = [];
      if (root) collect(root, all);
      else for (const el of byId.values()) collect(el, all);
      return [...new Set(all)].filter((el) => matchesSimple(el, simple));
    },
  };
  return document;
}

// ---------------- 测试夹具：加载真实 host.js ----------------

function reviewer(name, anthology_consent = false) {
  return {
    name,
    submitted: true,
    acknowledged: false,
    acknowledged_at: null,
    anthology_consent,
  };
}

function makeState(over = {}) {
  return {
    published: true,
    closed: false,
    can_publish: false,
    reviewers: NAMES.map((n) => reviewer(n)),
    reports: [],
    anthology_url: null,
    links: NAMES.map((n) => ({ name: n, url: "/p/token-" + n })),
    ...over,
  };
}

function jsonResponse(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

// getState：轮询时实时取最新状态；handlePost：POST 返回 {status, body}（可为异步）
function loadHost(getState, handlePost, { confirmResult = true } = {}) {
  const document = makeDocument();
  const requests = [];
  const sandbox = vm.createContext({
    console,
    document,
    location: { pathname: "/h/" + HOST_TOKEN, origin: "http://localhost:8000" },
    confirm: () => confirmResult,
    setInterval: () => 0, // 轮询由测试显式调用 refresh() 精确触发
    fetch: async (url, options = {}) => {
      if (!options.method || options.method === "GET") {
        return jsonResponse(200, getState());
      }
      const index = requests.length;
      requests.push({
        url,
        method: options.method,
        body: options.body ? JSON.parse(options.body) : null,
      });
      const result = await handlePost(url, options, index);
      return jsonResponse(result.status, result.body);
    },
  });
  vm.runInContext(HOST_JS, sandbox, { filename: "static/host.js" });

  return {
    document,
    requests,
    refresh: () => sandbox.refresh(),
    async flush(n = 5) {
      for (let i = 0; i < n; i++) await new Promise((r) => setImmediate(r));
    },
    checks() {
      return document.querySelectorAll("#anthology-members input[type=checkbox]");
    },
    checkedNames() {
      return this.checks()
        .filter((cb) => cb.checked)
        .map((cb) => cb.value);
    },
    consentCell(name) {
      const cb = this.checks().find((c) => c.value === name);
      return cb.closest("tr").children[2].textContent;
    },
    errorText() {
      return document.getElementById("anthology-error").textContent;
    },
    setChecked(name, value) {
      const cb = this.checks().find((c) => c.value === name);
      cb.checked = value;
      cb.dispatchEvent({ type: "change" });
    },
    clickPublish() {
      document.getElementById("anthology-publish").dispatchEvent({ type: "click" });
    },
  };
}

async function setup(getState, handlePost, opts) {
  const host = loadHost(getState, handlePost, opts);
  await host.flush(); // 脚本加载时的首次 refresh
  return host;
}

function withConsents(state, consents) {
  state.reviewers = NAMES.map((n) => reviewer(n, !!consents[n]));
}

// ---------------- 用例 ----------------

test("重复轮询：勾选保留，同时更新授权状态；撤回授权不缩减名单", async () => {
  const state = makeState();
  const host = await setup(() => state, async () => ({
    status: 200,
    body: { ok: true },
  }));

  host.setChecked("甲", true);
  host.setChecked("丙", true);

  // 第 1 次轮询：甲授权——勾选仍是甲、丙，授权列更新
  withConsents(state, { 甲: true });
  host.refresh();
  await host.flush();
  assert.deepEqual(host.checkedNames(), ["甲", "丙"]);
  assert.equal(host.consentCell("甲"), "已授权");
  assert.equal(host.consentCell("乙"), "未授权");
  assert.equal(host.consentCell("丙"), "未授权");

  // 第 2、3 次轮询：乙也授权；选择不变
  withConsents(state, { 甲: true, 乙: true });
  host.refresh();
  host.refresh();
  await host.flush();
  assert.deepEqual(host.checkedNames(), ["甲", "丙"]);
  assert.equal(host.consentCell("乙"), "已授权");

  // 成员撤回授权（甲）：勾选不得被静默缩减，授权列回到"未授权"，仍无发布请求
  withConsents(state, { 乙: true });
  host.refresh();
  await host.flush();
  assert.deepEqual(host.checkedNames(), ["甲", "丙"]);
  assert.equal(host.consentCell("甲"), "未授权");
  assert.equal(host.requests.length, 0);
});

test("取消勾选跨多次轮询保留", async () => {
  const state = makeState();
  const host = await setup(() => state, async () => ({
    status: 200,
    body: { ok: true },
  }));

  for (const n of NAMES) host.setChecked(n, true);
  host.setChecked("乙", false);
  for (let i = 0; i < 3; i++) {
    withConsents(state, { 丙: true });
    host.refresh();
  }
  await host.flush();
  assert.deepEqual(host.checkedNames(), ["甲", "丙"]);
  // 授权列仍在更新，未受勾选影响
  assert.equal(host.consentCell("丙"), "已授权");
});

test("空名单点击发布：只提示、不发请求", async () => {
  const state = makeState();
  const host = await setup(() => state, async () => {
    throw new Error("空名单不应发请求");
  });
  host.clickPublish();
  await host.flush();
  assert.equal(host.requests.length, 0);
  assert.match(host.errorText(), /请至少选择一名成员/);
  // 复选框仍可继续操作
  assert.ok(host.checks().every((cb) => !cb.disabled));
});

test("发布：按页面名单顺序提交勾选姓名到 anthology 接口与 names 字段", async () => {
  const state = makeState();
  withConsents(state, { 甲: true, 乙: true, 丙: true });
  const host = await setup(
    () => state,
    async (url) => {
      assert.equal(url, "/api/h/" + HOST_TOKEN + "/anthology");
      state.anthology_url = "/a/public-xyz";
      return { status: 200, body: { ok: true, url: "/a/public-xyz" } };
    }
  );

  // 故意先勾丙再勾甲（与页面顺序不同）
  host.setChecked("丙", true);
  host.setChecked("甲", true);
  host.clickPublish();
  await host.flush();

  assert.equal(host.requests.length, 1);
  assert.equal(host.requests[0].method, "POST");
  assert.deepEqual(host.requests[0].body, { names: ["甲", "丙"] });

  // 成功后显示公开链接、表单隐藏、停止编辑
  assert.equal(host.document.getElementById("anthology-form").hidden, true);
  assert.equal(host.document.getElementById("anthology-done").hidden, false);
  const link = host.document.getElementById("anthology-link");
  assert.equal(link.href, "http://localhost:8000/a/public-xyz");
  assert.equal(link.textContent, "http://localhost:8000/a/public-xyz");

  // 之后轮询到封存：仍是只读公开链接视图，表单不重新出现
  state.closed = true;
  host.refresh();
  await host.flush();
  assert.equal(host.document.getElementById("anthology-form").hidden, true);
  assert.equal(host.document.getElementById("anthology-done").hidden, false);
});

test("未授权整次拒绝：显示原因并保留勾选，调整（撤回/补授权）后可重试", async () => {
  const state = makeState();
  withConsents(state, { 甲: true, 丙: true }); // 乙未授权
  let attempt = 0;
  const host = await setup(
    () => state,
    async () => {
      attempt += 1;
      if (attempt === 1) {
        return {
          status: 409,
          body: {
            error: "有入选稿件未获授权，整次发布已取消",
            missing_consent: ["乙"],
          },
        };
      }
      state.anthology_url = "/a/pub2";
      return { status: 200, body: { ok: true, url: "/a/pub2" } };
    }
  );

  host.setChecked("甲", true);
  host.setChecked("乙", true);
  host.setChecked("丙", true);
  host.clickPublish();
  await host.flush();

  // 整次失败：无合辑（无成功请求）、勾选原样保留、错误展示未授权原因、控件恢复可调整
  assert.equal(attempt, 1);
  assert.deepEqual(host.checkedNames(), ["甲", "乙", "丙"]);
  assert.match(host.errorText(), /未授权[^乙]*乙/);
  assert.equal(
    host.document.getElementById("anthology-publish").disabled,
    false
  );
  assert.ok(host.checks().every((cb) => !cb.disabled));

  // 主持人可调整名单（取消乙）——也保留选择能力；这里改由乙补授权后用原名单重试
  withConsents(state, { 甲: true, 乙: true, 丙: true });
  host.refresh();
  await host.flush();
  assert.deepEqual(host.checkedNames(), ["甲", "乙", "丙"]);
  host.clickPublish();
  await host.flush();

  assert.equal(attempt, 2);
  assert.equal(host.requests.length, 2);
  assert.deepEqual(host.requests[1].body, { names: ["甲", "乙", "丙"] });
  assert.equal(host.document.getElementById("anthology-form").hidden, true);
  assert.match(
    host.document.getElementById("anthology-link").textContent,
    /\/a\/pub2/
  );
});

test("撤回授权后的失败重试：不勾选撤回者即可成功，两次名单分别固化", async () => {
  const state = makeState();
  withConsents(state, { 甲: true, 乙: true });
  let attempt = 0;
  const host = await setup(
    () => state,
    async () => {
      attempt += 1;
      if (attempt === 1) {
        return {
          status: 409,
          body: { error: "x", missing_consent: ["丙"] },
        };
      }
      state.anthology_url = "/a/pub3";
      return { status: 200, body: { ok: true, url: "/a/pub3" } };
    }
  );

  host.setChecked("甲", true);
  host.setChecked("乙", true);
  host.setChecked("丙", true);
  host.clickPublish();
  await host.flush();
  assert.equal(attempt, 1);
  // 失败后取消勾选撤回者丙，再试
  host.setChecked("丙", false);
  assert.deepEqual(host.checkedNames(), ["甲", "乙"]);
  host.clickPublish();
  await host.flush();
  assert.equal(attempt, 2);
  assert.deepEqual(host.requests[1].body, { names: ["甲", "乙"] });
  assert.equal(host.document.getElementById("anthology-done").hidden, false);
});

test("状态变为封存：选择与发布被禁用，勾选不丢但无法再发请求", async () => {
  const state = makeState();
  withConsents(state, { 甲: true, 乙: true });
  const host = await setup(() => state, async () => ({
    status: 409,
    body: { error: "不应在封存后发请求" },
  }));
  host.setChecked("甲", true);
  host.setChecked("乙", true);

  // 轮询到封存：published=true、closed=true
  state.closed = true;
  host.refresh();
  await host.flush();

  assert.ok(host.checks().every((cb) => cb.disabled));
  assert.deepEqual(host.checkedNames(), ["甲", "乙"]); // 禁用不清空
  assert.equal(host.document.getElementById("anthology-publish").disabled, true);

  // 点击被禁用的发布按钮（浏览器不会触发）：无请求
  const before = host.requests.length;
  host.clickPublish();
  await host.flush();
  assert.equal(host.requests.length, before);
});

test("发布请求进行中再次轮询：勾选冻结保留，完成后转只读", async () => {
  const state = makeState();
  withConsents(state, { 甲: true, 乙: true, 丙: true });
  let release;
  const gate = new Promise((r) => {
    release = r;
  });
  const host = await setup(
    () => state,
    async () => {
      await gate;
      state.anthology_url = "/a/pub4";
      return { status: 200, body: { ok: true, url: "/a/pub4" } };
    }
  );

  host.setChecked("乙", true);
  host.setChecked("丙", true);
  host.clickPublish();
  // 请求挂起期间先让点击处理跑到 await fetch
  await host.flush(2);

  // 进行中：所有复选框与发布按钮禁用
  assert.ok(host.checks().every((cb) => cb.disabled));
  assert.equal(host.document.getElementById("anthology-publish").disabled, true);

  // 进行中再来一次轮询：重绘后仍禁用，勾选不丢
  host.refresh();
  await host.flush(2);
  assert.ok(host.checks().every((cb) => cb.disabled));
  assert.deepEqual(host.checkedNames(), ["乙", "丙"]);
  assert.equal(host.requests.length, 1);
  assert.deepEqual(host.requests[0].body, { names: ["乙", "丙"] });

  release();
  await host.flush();
  assert.equal(host.document.getElementById("anthology-form").hidden, true);
  assert.match(
    host.document.getElementById("anthology-link").textContent,
    /\/a\/pub4/
  );
});
