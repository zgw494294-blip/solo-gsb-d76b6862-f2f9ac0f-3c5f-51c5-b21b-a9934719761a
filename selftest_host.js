"use strict";
/**
 * 主持页匿名合辑勾选的前端回归自测（不依赖浏览器与运行中的服务）：
 * 用最小 DOM 模拟在 Node 的 vm 中加载 static/host.js，验证 3 秒轮询不再清空勾选。
 *
 * 覆盖：
 * - 重复轮询：勾选在多次轮询重绘后保留，且各人授权状态仍随轮询更新；
 * - 取消勾选：取消的结果同样被保留，不会被轮询恢复成勾选；
 * - 勾选后跨轮询发布：提交勾选姓名，而不是提示未选成员；
 * - 授权撤回：只更新该成员的授权显示，不静默缩减所选名单；发布仍提交该姓名，
 *   由服务端整次 409 拒绝并显示原因，失败后保留勾选供调整重试；
 * - 空名单：提示且不发请求；
 * - 发布成功：按页面名单顺序提交勾选姓名，成功后显示公开链接并停止编辑；
 * - 只读转换：会话封存后勾选框与发布按钮均禁用，已发合辑只显示公开链接。
 *
 * 运行：node selftest_host.js
 */

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const HOST_JS = fs.readFileSync(path.join(__dirname, "static", "host.js"), "utf8");

// host.js 运行期间会按 id 取用的全部元素
const ELEMENT_IDS = [
  "error", "publish", "content", "fatal",
  "published-banner", "closed-banner", "close-section",
  "pending-summary", "ack-head", "ack-note", "reviewers",
  "reports-section", "reports", "report-decision-error",
  "anthology-section", "anthology-form", "anthology-done",
  "anthology-members", "anthology-publish", "anthology-link", "anthology-error",
  "links", "rotate-error", "remove-error",
  "close-session", "close-error",
];

class El {
  constructor(tag) {
    this.tagName = String(tag || "div").toUpperCase();
    this.children = [];
    this.dataset = {};
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.value = "";
    this.type = "";
    this.className = "";
    this.textContent = "";
    this.href = "";
    this._listeners = Object.create(null);
    this._innerHTML = "";
  }

  set innerHTML(v) {
    this._innerHTML = v;
    this.children = []; // 模拟重绘：清空子节点
  }

  get innerHTML() {
    return this._innerHTML;
  }

  appendChild(child) {
    this.children.push(child);
    return child;
  }

  append(...nodes) {
    this.children.push(...nodes);
  }

  addEventListener(type, fn) {
    (this._listeners[type] = this._listeners[type] || []).push(fn);
  }

  dispatch(type, extra) {
    const event = Object.assign({ target: this }, extra);
    for (const fn of this._listeners[type] || []) fn(event);
  }

  closest() {
    return null;
  }
}

// 等待 host.js 中的异步 refresh/点击处理链全部落定
async function flush() {
  for (let i = 0; i < 20; i++) await new Promise(setImmediate);
}

function hostState(overrides = {}) {
  const base = {
    published: true,
    closed: false,
    can_publish: false,
    reviewers: [
      { name: "甲", submitted: true, acknowledged: false, acknowledged_at: null, anthology_consent: true },
      { name: "乙", submitted: true, acknowledged: false, acknowledged_at: null, anthology_consent: false },
      { name: "丙", submitted: true, acknowledged: false, acknowledged_at: null, anthology_consent: true },
    ],
    reports: [],
    anthology_url: null,
    links: ["甲", "乙", "丙"].map((n, i) => ({ name: n, url: "/p/t" + (i + 1) })),
  };
  return Object.assign(base, overrides);
}

// 在模拟 DOM 中加载 host.js：env.state 为每次 GET 轮询返回的状态（可随时替换），
// env.postResponse 为下一次 POST /anthology 的响应
function createPage() {
  const elements = {};
  for (const id of ELEMENT_IDS) elements[id] = new El("div");
  const env = {
    elements,
    fetchCalls: [],
    state: hostState(),
    postResponse: null,
    confirmResult: true,
    poll: null,
  };
  const document = {
    getElementById: (id) => elements[id] || null,
    createElement: (tag) => new El(tag),
  };
  const sandbox = {
    document,
    location: { pathname: "/h/hosttok", origin: "http://localhost:8000" },
    fetch: async (url, opts) => {
      const call = {
        url,
        method: (opts && opts.method) || "GET",
        body: opts && opts.body ? JSON.parse(opts.body) : undefined,
      };
      env.fetchCalls.push(call);
      if (call.method === "GET") {
        return { ok: true, json: async () => env.state };
      }
      const resp = env.postResponse || { ok: false, body: { error: "未配置 POST 响应" } };
      return { ok: resp.ok, json: async () => resp.body };
    },
    confirm: () => env.confirmResult,
    alert: () => {},
    setInterval: (cb) => {
      env.poll = cb;
      return 1;
    },
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(HOST_JS, sandbox, { filename: "host.js" });
  return env;
}

// ---------------- 页面操作辅助 ----------------

function rows(env) {
  return env.elements["anthology-members"].children;
}

function checkbox(env, name) {
  for (const tr of rows(env)) {
    const cb = tr.children[0].children[0];
    if (cb.value === name) return cb;
  }
  return null;
}

function checkedNames(env) {
  return rows(env)
    .map((tr) => tr.children[0].children[0])
    .filter((cb) => cb.checked)
    .map((cb) => cb.value);
}

function consentText(env, name) {
  for (const tr of rows(env)) {
    if (tr.children[1].textContent === name) return tr.children[2].textContent;
  }
  return null;
}

async function setChecked(env, name, on) {
  const cb = checkbox(env, name);
  assert.ok(cb, `应存在「${name}」的勾选框`);
  cb.checked = on;
  cb.dispatch("change");
  await flush();
}

// 模拟一次 3 秒轮询（可先替换返回状态）
async function poll(env, state) {
  if (state) env.state = state;
  assert.ok(env.poll, "页面应已注册轮询");
  env.poll();
  await flush();
}

async function clickPublish(env) {
  env.elements["anthology-publish"].dispatch("click");
  await flush();
}

function posts(env) {
  return env.fetchCalls.filter((c) => c.method === "POST");
}

// ---------------- 用例 ----------------

const tests = [];
function test(name, fn) {
  tests.push([name, fn]);
}

test("重复轮询保留勾选，且各人授权状态仍随轮询更新", async () => {
  const env = createPage();
  await flush();
  await setChecked(env, "甲", true);
  await setChecked(env, "丙", true);
  assert.deepEqual(checkedNames(env), ["甲", "丙"]);
  // 多次 3 秒轮询：勾选不丢
  await poll(env);
  await poll(env);
  assert.deepEqual(checkedNames(env), ["甲", "丙"], "重复轮询不得清空勾选");
  // 轮询仍更新各人授权：乙授权后下一次轮询显示已授权
  const s = hostState();
  s.reviewers[1].anthology_consent = true;
  await poll(env, s);
  assert.equal(consentText(env, "乙"), "已授权");
  assert.deepEqual(checkedNames(env), ["甲", "丙"], "授权变化不得改动勾选");
});

test("取消勾选的结果同样被轮询保留", async () => {
  const env = createPage();
  await flush();
  await setChecked(env, "甲", true);
  await setChecked(env, "丙", true);
  await poll(env);
  // 取消 甲：随后的轮询不得把它恢复成勾选
  await setChecked(env, "甲", false);
  await poll(env);
  await poll(env);
  assert.deepEqual(checkedNames(env), ["丙"], "取消勾选不得被轮询恢复");
});

test("勾选后跨轮询点击发布：提交勾选姓名而非提示未选成员", async () => {
  const env = createPage();
  await flush();
  await setChecked(env, "甲", true);
  await setChecked(env, "乙", true);
  await poll(env); // 关键路径：勾选后经历一次 3 秒轮询再发布
  env.postResponse = { ok: true, body: { ok: true, url: "/a/pub1" } };
  env.state = hostState({ anthology_url: "/a/pub1" });
  await clickPublish(env);
  assert.equal(env.elements["anthology-error"].textContent, "", "不得提示未选成员");
  assert.equal(posts(env).length, 1, "应发出一次发布请求");
  assert.equal(posts(env)[0].url, "/api/h/hosttok/anthology");
  assert.deepEqual(posts(env)[0].body, { names: ["甲", "乙"] });
});

test("发布按页面名单顺序提交（与勾选先后无关）", async () => {
  const env = createPage();
  await flush();
  // 先勾 丙 再勾 甲：提交顺序仍按页面名单顺序（甲在丙前）
  await setChecked(env, "丙", true);
  await setChecked(env, "甲", true);
  await poll(env);
  env.postResponse = { ok: true, body: { ok: true, url: "/a/pub2" } };
  env.state = hostState({ anthology_url: "/a/pub2" });
  await clickPublish(env);
  assert.equal(posts(env).length, 1);
  assert.deepEqual(posts(env)[0].body, { names: ["甲", "丙"] }, "按页面名单顺序提交");
});

test("成员撤回授权不静默缩减所选名单；整次失败后保留勾选可重试", async () => {
  const env = createPage();
  await flush();
  await setChecked(env, "甲", true);
  await setChecked(env, "丙", true);
  // 丙撤回授权：下一次轮询显示未授权，但勾选不被静默移除
  const s = hostState();
  s.reviewers[2].anthology_consent = false;
  await poll(env, s);
  assert.equal(consentText(env, "丙"), "未授权");
  assert.deepEqual(checkedNames(env), ["甲", "丙"], "撤回授权不得静默缩减所选名单");
  // 发布仍提交丙，由服务端整次拒绝并显示原因
  env.postResponse = {
    ok: false,
    body: { error: "有入选稿件未获授权，整次发布已取消", missing_consent: ["丙"] },
  };
  await clickPublish(env);
  assert.equal(posts(env).length, 1);
  assert.deepEqual(posts(env)[0].body, { names: ["甲", "丙"] });
  assert.ok(
    env.elements["anthology-error"].textContent.includes("未授权：丙"),
    "应显示服务端返回的未授权原因"
  );
  // 失败后保留勾选、按钮恢复可用，供调整重试
  assert.deepEqual(checkedNames(env), ["甲", "丙"], "失败后保留选择");
  assert.equal(env.elements["anthology-publish"].disabled, false);
  // 丙重新授权后按原名单重试成功：显示公开链接并停止编辑
  env.postResponse = { ok: true, body: { ok: true, url: "/a/pub3" } };
  env.state = hostState({ anthology_url: "/a/pub3" });
  await clickPublish(env);
  assert.equal(posts(env).length, 2);
  assert.deepEqual(posts(env)[1].body, { names: ["甲", "丙"] }, "重试仍按原勾选提交");
  assert.equal(env.elements["anthology-form"].hidden, true, "发布后停止编辑");
  assert.equal(env.elements["anthology-done"].hidden, false);
  assert.equal(
    env.elements["anthology-link"].textContent,
    "http://localhost:8000/a/pub3"
  );
});

test("空名单提示且不发请求", async () => {
  const env = createPage();
  await flush();
  await clickPublish(env);
  assert.equal(env.elements["anthology-error"].textContent, "请至少选择一名成员。");
  assert.equal(posts(env).length, 0, "空名单不得发请求");
  // 勾选再全部取消后同样提示且不发请求
  await setChecked(env, "甲", true);
  await setChecked(env, "甲", false);
  await poll(env);
  await clickPublish(env);
  assert.equal(env.elements["anthology-error"].textContent, "请至少选择一名成员。");
  assert.equal(posts(env).length, 0);
});

test("会话封存后禁用选择与发布（只读转换）", async () => {
  const env = createPage();
  await flush();
  await setChecked(env, "甲", true);
  await poll(env, hostState({ closed: true }));
  const cbs = rows(env).map((tr) => tr.children[0].children[0]);
  assert.equal(cbs.length, 3);
  assert.ok(cbs.every((cb) => cb.disabled), "封存后勾选框全部禁用");
  assert.equal(env.elements["anthology-publish"].disabled, true, "封存后发布按钮禁用");
  // 已发合辑且封存：只显示公开链接（只读），不再显示选择表单
  await poll(env, hostState({ closed: true, anthology_url: "/a/readonly1" }));
  assert.equal(env.elements["anthology-form"].hidden, true);
  assert.equal(env.elements["anthology-done"].hidden, false);
  assert.equal(
    env.elements["anthology-link"].textContent,
    "http://localhost:8000/a/readonly1"
  );
});

(async () => {
  let failed = 0;
  for (const [name, fn] of tests) {
    try {
      await fn();
      console.log("ok - " + name);
    } catch (e) {
      failed++;
      console.error("FAIL - " + name);
      console.error(e);
    }
  }
  if (failed) {
    console.error(failed + " 个用例失败");
    process.exit(1);
  }
  console.log(tests.length + " 个用例全部通过");
})();
