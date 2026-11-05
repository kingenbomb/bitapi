// Run the production Vue compiler and account component with a minimal renderer.
// Fixtures below are fictional. This checks rendering/handlers, not browser layout.
const fs = require("fs");
const vm = require("vm");
const assert = require("assert/strict");
const path = require("path");
const repo = path.resolve(__dirname, "..");
process.on("uncaughtException", error => { console.error(error.stack); process.exit(1); });
const context = { console, window: {},
  localStorage: { getItem: () => null, setItem() {} },
  setTimeout, clearTimeout, AbortController, TextDecoder, setInterval: () => 0, clearInterval() {} };
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(repo, "static/vue.global.prod.js"), "utf8"), context);
const V = context.Vue;
context.document = { hidden: false };
const passthrough = { render() { return V.h("div", null, this.$slots.default ? this.$slots.default() : []); } };
const modal = { props: ["show"], render() { return this.show ? V.h("div", null, this.$slots.default?.()) : null; } };
const table = { props: ["data", "columns"], render() {
  return V.h("table", null, (this.data || []).map(row => V.h("tr", { key: row.id },
    (this.columns || []).map(col => V.h("td", null, col.render ? col.render(row) : String(row[col.key] ?? ""))))));
} };
const image = { props: ["src", "alt"], render() { return V.h("img", { src: this.src, alt: this.alt }); } };
const naive = new Proxy({ NModal: modal, NDrawer: modal, NDataTable: table, NImage: image }, {
  get(object, key) { return object[key] || passthrough; }
});
context.naive = context.window.naive = naive;
const usage = n => ({ requests: n, tokens: n * 100, cost: n / 10, user_cost: n / 5 });
const row = (id, requests) => ({ id, name: "Fixture account " + id, identity: "fixture-" + id + "@example.test",
  channel: "gpt", account_type: "oauth", pool_kind: "account", status: "active", status_label: "可用", paused: false,
  priority: 50, concurrency: 2, inflight: 0, rate_multiplier: 1, group_ids: [], created_at: 1,
  today: usage(requests), week: usage(requests), total_usage: usage(requests), capabilities: ["chat"], models: ["fixture-model"],
  credential_fields: ["api_key"], note: "", auto_pause_on_expired: true, account_expires_at: 0,
  quota_limit: 0, quota_daily_limit: 0, quota_weekly_limit: 0, proxy_id: null,
  email: "fixture-" + id + "@example.test", plan_type: "plus", can_usage: true,
  gpt_sync: { attempted_at: Math.floor(Date.now() / 1000), updated_at: Math.floor(Date.now() / 1000) },
  upstream_usage: { five_hour: null, seven_day: { utilization: 18, reset_at: Math.floor(Date.now() / 1000) + 86400 },
    credits: { has_credits: false, unlimited: false, balance: null }, allowed: true, limit_reached: false },
  gpt_reset_credits: { available_count: 1, expirations: [1930000000] },
  account_expires_at: 1900000000, subscription_expires_at: 1910000000, expires_at: 1920000000 });
const fixtures = [row(1, 892), row(2, 7), row(3, 0)];
fixtures[0].last_error = "HTTP 503 from upstream: Our servers are currently overloaded";
fixtures[0].last_error_kind = "capacity";
const keyFixture = { ...row(4, 0), channel: "fixture", pool_kind: "key", account_type: "apikey", can_usage: false };
const allFixtures = [...fixtures, keyFixture];
const calls = [];
let nextPreview = null;
let nextSync = null;
let nextTest = null, nextCatalog = null, nextProxyTest = null;
let availableModels = [{ id: "fixture-model", test_status: "verified" }, { id: "fixture-new-model", test_status: "busy" },
  ...Array.from({ length: 6 }, (_, index) => ({ id: "fixture-upstream-" + index, test_status: "untested" })),
  { id: "gpt-reserve", hidden: true, test_status: "verified" }, { id: "codex-auto-review", hidden: true, test_status: "untested" }];
const api = async (method, url, body, options) => {
  calls.push({ method, url, body, options });
  const pathname = url.split("?")[0];
  const query = new URLSearchParams(url.split("?")[1]);
  const matching = allFixtures.filter(r => (!query.get("pool_kind") || r.pool_kind === query.get("pool_kind")) &&
    (!query.get("channel") || r.channel === query.get("channel")));
  if (pathname.endsWith("/options")) return { channels: [{ name: "gpt", pool_kinds: ["account"], loaded: true, can_proxy: true },
    { name: "fixture", pool_kinds: ["key"], loaded: true, can_proxy: true }], groups: [], proxies: [] };
  if (pathname.endsWith("/summary")) return { totals: { total: matching.length, active: matching.length },
    categories: { account: 3, key: 1 }, channels: [], unassigned_today: 1, tracking_since: 1 };
  if (pathname.endsWith("/preview")) {
    if (nextPreview) { const pending = nextPreview; nextPreview = null; return pending; }
    const targets = allFixtures.filter(r => (!body.ids || body.ids.includes(r.id)) &&
      (!body.filters?.pool_kind || body.filters.pool_kind === r.pool_kind)).map(r => ({ id: r.id, name: r.name, version: "v" }));
    return { count: targets.length, targets };
  }
  if (pathname.endsWith("/bulk")) return { results: body.ids.map(id => ({ id, ok: true })), success: body.ids.length, failed: 0 };
  if (pathname.endsWith("/stats")) return { summary: usage(892), series: [{ day: "2026-11-02", requests: 892 }], models: [], recent: [], windows: { "5h": usage(1), "7d": usage(2) } };
  if (pathname.endsWith("/tests")) return { history: [], plan: null };
  if (pathname.endsWith("/models/detect")) {
    return { models: availableModels, source: "upstream", verified_count: 2, allow_custom: true, scan: { running: false, done: 10, total: 10 } };
  }
  if (pathname.endsWith("/models") && nextCatalog) { const pending = nextCatalog; nextCatalog = null; return pending; }
  if (pathname.endsWith("/models")) return { models: availableModels, source: "upstream", verified_count: 2, allow_custom: true, scan: { running: false }, fetched_at: 1 };
  if (/\/proxies\/\d+\/test$/.test(pathname)) {
    if (nextProxyTest) { const pending = nextProxyTest; nextProxyTest = null; return pending; }
    return { ok: true, latency_ms: 123, elapsed_ms: 123, http_status: 401, message: "Connected fixture" };
  }
  if (pathname.endsWith("/test") && nextTest) { const pending = nextTest; nextTest = null; return pending; }
  if (pathname.endsWith("/test")) return { ok: true, reply: "Hello from the selected model!", prompt: "hi", model: body.model, elapsed_ms: 125 };
  if (pathname.endsWith("/sync")) {
    if (nextSync) { const pending = nextSync; nextSync = null; return pending; }
    return { ok: true, account: fixtures[0] };
  }
  if (/\/accounts\/\d+$/.test(pathname)) return { account: fixtures[0] };
  return { accounts: matching, total: matching.length, generated_at: 1 };
};
context.window.BitPortal = { api, qs: value => "?" + new URLSearchParams(Object.entries(value).filter(([, v]) => v != null && v !== "")), msg: { success() {}, error(e) { throw Error(e); } },
  BASE: "", token: () => "cookie", authHeaders: () => ({ "X-CSRF-Token": "fixture-csrf" }), clearToken() {},
  nf: n => String(n || 0), usd: n => "$" + Number(n || 0).toFixed(4), MONO: {}, C: {},
  ic: name => V.h("svg", { "data-icon": name }),
  Ic: { props: ["name"], render() { return V.h("svg", { "data-icon": this.name }); } },
  usageHint: (label, content, options = {}) => V.h("button", { "aria-label": label, class: options.class }, content),
  stack: (a, b) => V.h("div", null, [a, V.h("small", null, String(b ?? ""))]),
  tag: (type, label) => { assert(["success", "warning", "error", "info", "default"].includes(type)); return V.h("span", null, label); },
  absTime: n => String(n), relTime: n => String(n), BarChart: passthrough };
context.window.BitPortalPages = { Load: passthrough };
context.window.BitPortalAdmin = { AdminIntro: passthrough, Admin: { components: {} } };
vm.runInContext(fs.readFileSync(path.join(repo, "static/portal-gpt.js"), "utf8"), context);
vm.runInContext(fs.readFileSync(path.join(repo, "static/portal-pool.js"), "utf8"), context);
const component = context.window.BitPortalPool.Pool;
assert.equal(context.window.BitPortalAdmin.Admin.components.Pool, component);
component.render = V.compile(component.template, { decodeEntities: text => text });
const node = (type, text = "") => ({ type, text, children: [], props: {}, parent: null });
const renderer = V.createRenderer({
  createElement: type => node(type), createText: text => node("text", text), createComment: text => node("comment", text),
  setText: (n, text) => { n.text = text; }, setElementText: (n, text) => { n.text = text; n.children = []; },
  patchProp: (n, key, oldValue, value) => { n.props[key] = value; },
  insert(n, parent, anchor = null) {
    if (n.parent) { const i = n.parent.children.indexOf(n); if (i >= 0) n.parent.children.splice(i, 1); }
    n.parent = parent; const i = anchor ? parent.children.indexOf(anchor) : -1;
    if (i < 0) parent.children.push(n); else parent.children.splice(i, 0, n);
  },
  remove(n) { if (n.parent) { const i = n.parent.children.indexOf(n); if (i >= 0) n.parent.children.splice(i, 1); } },
  parentNode: n => n.parent, nextSibling: n => n.parent?.children[n.parent.children.indexOf(n) + 1] || null,
});
const app = renderer.createApp(component);
for (const name of new Set((component.template.match(/<n-[a-z-]+/g) || []).map(x => x.slice(1)))) {
  const pascal = name.split("-").map(s => s[0].toUpperCase() + s.slice(1)).join("");
  app.component(name, naive[pascal]);
}
app.config.errorHandler = error => { throw error; };
const target = node("root");
const textOf = n => n.text + n.children.map(textOf).join(" ");
(async () => {
  const shared = fs.readFileSync(path.join(repo, "static/portal-shared.js"), "utf8");
  const apiSource = shared.slice(shared.indexOf("  function api("), shared.indexOf("  /* 查询串"));
  for (const phase of ["headers", "body"]) {
    const timed = { BASE: "", AbortController, setTimeout, clearTimeout, token: () => "cookie", authHeaders: () => ({}), clearToken() {}, location: {} };
    timed.fetch = (_, options) => phase === "headers"
      ? new Promise((_, reject) => options.signal.addEventListener("abort", () => reject(new Error("aborted"))))
      : Promise.resolve({ ok: true, status: 200, json: () => new Promise((_, reject) => options.signal.addEventListener("abort", () => reject(new Error("aborted body")))) });
    vm.createContext(timed); vm.runInContext(apiSource, timed);
    await assert.rejects(timed.api("POST", "/fixture", {}, { timeout: 10 }), /测试请求等待超时/);
  }
  const instance = app.mount(target);
  await new Promise(resolve => setImmediate(resolve)); await V.nextTick();
  assert(textOf(target).includes("Fixture account 1"), "account name must render");
  assert(textOf(target).includes("892"));
  assert(textOf(target).includes("7"));
  for (const expected of ["账号池", "Key 池", "Plus", "5h", "7d", "18%", "1910000000", "1920000000"]) {
    assert(textOf(target).includes(expected), "GPT row must render " + expected);
  }
  assert(!textOf(target).includes("次窗口未提供"));
  const nodesOf = n => [n, ...n.children.flatMap(nodesOf)];
  const initialNodes = nodesOf(target);
  assert.equal(initialNodes.filter(n => n.props.class === "pool-gpt-window-hint").length, 3,
    "only the three actual 7d windows should render bars; missing 5h must not get a fake bar");
  assert(initialNodes.some(n => String(n.props["aria-label"]).includes("可用额度重置券：1 张")));
  assert(initialNodes.some(n => String(n.props["aria-label"]).includes("Codex 积分余额：0")));
  assert(initialNodes.some(n => String(n.props["aria-label"]).includes("未返回 5h")));
  assert.equal(calls.filter(call => call.url.endsWith("/sync")).length, 0, "fresh snapshot must not refetch upstream");
  assert.equal(instance.total, 3);
  instance.options.proxies = [{ id: 77, name: "Fixture proxy", address: "http://proxy.example.test:8080", accounts: 0 }];
  instance.proxy.show = true; await V.nextTick();
  assert(!textOf(target).includes("输入格式"));
  assert(!textOf(target).includes("代理协议"));
  assert.equal(instance.proxy.name, "");
  instance.proxy.name = "  My proxy  ";
  instance.proxy.url = "proxy.example.test:8080:fixture:secret";
  await instance.saveProxy();
  const savedProxy = calls.find(call => call.url.endsWith("/proxies") && call.method === "POST");
  assert.deepEqual(JSON.parse(JSON.stringify(savedProxy.body)), { name: "My proxy", url: "proxy.example.test:8080:fixture:secret" });
  assert.equal(instance.proxy.name, "");
  assert.equal(instance.proxy.url, "");
  instance.options.proxies = [{ id: 77, name: "Fixture proxy", address: "http://proxy.example.test:8080", accounts: 0 }];
  await V.nextTick();
  assert(textOf(target).includes("测试 GPT"));
  let resolveProxy;
  nextProxyTest = new Promise(resolve => { resolveProxy = resolve; });
  const proxyRequest = instance.testProxy(77);
  assert.equal(instance.proxyTests[77].running, true);
  await instance.testProxy(77);
  assert.equal(calls.filter(call => call.url.endsWith("/proxies/77/test")).length, 1);
  assert.equal(calls.find(call => call.url.endsWith("/proxies/77/test")).options.timeout, 15000);
  resolveProxy({ ok: true, latency_ms: 123, message: "Connected fixture" });
  await proxyRequest; await V.nextTick();
  assert.equal(instance.proxyTests[77].running, false);
  assert.equal(instance.proxyTests[77].latency_ms, 123);
  nextProxyTest = Promise.reject(new Error("fixture connection failure"));
  await instance.testProxy(77); await V.nextTick();
  assert.equal(instance.proxyTests[77].running, false);
  assert.equal(instance.proxyTests[77].ok, false);
  assert(textOf(target).includes("fixture connection failure"));
  instance.proxy.show = false;
  await instance.selectAllResults();
  assert.equal(instance.selected.length, 3);
  instance.openEditor(instance.rows[0]); await V.nextTick();
  assert(textOf(target).includes("访问凭据") || textOf(target).includes("更新凭据"));
  instance.editor.show = false;
  await instance.openBatch("pause"); await V.nextTick();
  assert.equal(instance.batch.targets.length, 3);
  await instance.runBatch(); await V.nextTick();
  assert.equal(instance.batch.done, 3);
  assert.equal(instance.selected.length, 0);
  const request = calls.find(call => call.url.endsWith("/bulk"));
  assert.equal(request.body.ids.length, 3);
  assert.equal(Object.keys(request.body.versions).length, 3);
  assert.equal(request.body.pool_kind, "account");
  instance.batch.show = false;
  await instance.openDetails(instance.rows[0], "stats"); await V.nextTick();
  assert.equal(instance.detail.stats.summary.requests, 892);
  instance.detail.stats.summary.requests = 0; await V.nextTick();
  assert(textOf(target).includes("还没有本站调用记录"));
  assert(!nodesOf(target).some(n => n.type === "h3" && textOf(n).includes("模型分布")));
  assert(!nodesOf(target).some(n => n.type === "h3" && textOf(n).includes("最近请求")));
  assert(textOf(target).includes("上游繁忙"));
  instance.openTest(instance.rows[0]);
  await new Promise(resolve => setImmediate(resolve)); await V.nextTick();
  assert.equal(instance.modelsFor(instance.test.row).length, 10);
  assert.equal(instance.test.model, "fixture-model");
  const hiddenOption = instance.modelsFor(instance.test.row).find(option => option.value === "codex-auto-review");
  assert(hiddenOption);
  assert.equal(hiddenOption.disabled, undefined);
  assert(textOf(target).includes("可选 10 个型号"));
  assert(!calls.some(call => call.url.endsWith("/models/detect")));
  await instance.loadTestModels(instance.test.row, true); await V.nextTick();
  assert.equal(instance.modelsFor(instance.test.row).length, 10);
  assert.equal(instance.test.model, "fixture-model");
  assert.equal(calls.find(call => call.url.endsWith("/models/detect")).method, "POST");
  instance.test.model = "manual-fixture-model";
  await instance.runTest(); await V.nextTick();
  assert.equal(instance.test.result.reply, "Hello from the selected model!");
  assert(textOf(target).includes("Hello from the selected model!"));
  assert.equal(calls.find(call => /\/accounts\/\d+\/test$/.test(call.url)).body.model, "manual-fixture-model");
  assert.equal(calls.find(call => /\/accounts\/\d+\/test$/.test(call.url)).options.timeout, 35000);
  let finishTest, finishCatalog;
  nextTest = new Promise(resolve => { finishTest = resolve; });
  const testing = instance.runTest();
  await V.nextTick();
  assert.equal(instance.test.running, true);
  instance.test.elapsed = 12; await V.nextTick();
  assert(textOf(target).includes("测试中 · 12 秒"));
  nextCatalog = new Promise(resolve => { finishCatalog = resolve; });
  finishTest({ ok: false, error: "账号测试超时", error_code: 408 });
  await testing; await V.nextTick();
  assert.equal(instance.test.running, false, "a stalled catalog refresh must not keep the test spinning");
  assert(textOf(target).includes("账号测试超时"));
  finishCatalog({ models: availableModels, image_models: [{ id: "gpt-image-2-web", name: "ChatGPT Web 生图", backend: "web", test_status: "untested" }], source: "verified", scan: { running: false } });
  await new Promise(resolve => setImmediate(resolve));
  instance.setTestKind("image");
  assert.equal(instance.test.model, "gpt-image-2-web");
  assert(instance.modelsFor(instance.test.row, "image")[0].label.includes("ChatGPT Web 生图"));
  assert(!instance.modelsFor(instance.test.row).some(option => option.value === "gpt-image-2-web"));
  instance.test.prompt = "A fictional orange cat";
  let imageRequest;
  context.fetch = async (url, options) => {
    imageRequest = { url, options };
    const frames = ': keepalive\n\nevent: progress\ndata: {"message":"正在生成图片"}\n\nevent: result\ndata: ' +
      JSON.stringify({ ok: true, kind: "image", model: "fixture-model", elapsed_ms: 37000, images: [{ mime_type: "image/png", b64_json: "fixture-pixels" }] }) + '\n\n';
    const bytes = new TextEncoder().encode(frames); let position = 0;
    return { ok: true, body: { getReader: () => ({ read: async () => position >= bytes.length ? { done: true } :
      { done: false, value: bytes.slice(position, position += 7) }, cancel: async () => {} }) } };
  };
  await instance.runTest(); await V.nextTick();
  assert.equal(instance.test.running, false);
  assert.equal(imageRequest.url, "/api/admin/pool/accounts/1/test-image");
  assert.equal(JSON.parse(imageRequest.options.body).prompt, "A fictional orange cat");
  assert.equal(JSON.parse(imageRequest.options.body).model, "gpt-image-2-web");
  assert(nodesOf(target).some(n => n.type === "img" && n.props.src === "data:image/png;base64,fixture-pixels"));
  assert(textOf(target).includes("点击图片放大查看"));
  assert(!textOf(nodesOf(target).find(n => n.props.class === "pool-test-conversation")).includes("hi"));
  context.fetch = async () => ({ ok: true, body: { getReader: () => ({ read: async () => ({ done: true }), cancel: async () => {} }) } });
  await instance.runTest(); await V.nextTick();
  assert.equal(instance.test.running, false, "an interrupted image stream must release the test button");
  assert(instance.test.error.includes("未收到测试结果"));
  assert.equal(instance.test.result, null);
  instance.setTestKind("chat"); await V.nextTick();
  assert.equal(instance.test.result, null);
  instance.test.show = false;
  instance.detail.show = false;
  instance.clearSelection();
  let resolveStaleSelection;
  nextPreview = new Promise(resolve => { resolveStaleSelection = resolve; });
  const selecting = instance.selectAllResults();
  instance.filters.q = "changed filter";
  await V.nextTick();
  resolveStaleSelection({ targets: [{ id: 99 }], count: 1 });
  await selecting;
  assert.equal(instance.selected.length, 0, "stale selection must not cross filter scopes");
  let resolveOldPreview;
  nextPreview = new Promise(resolve => { resolveOldPreview = resolve; });
  const oldPreview = instance.openBatch("delete", [1]);
  await instance.openBatch("pause", [2]);
  resolveOldPreview({ targets: [{ id: 1 }], count: 1 });
  await oldPreview;
  assert.equal(instance.batch.action, "pause");
  assert.equal(instance.batch.targets[0].id, 2, "late preview must not replace the current operation targets");
  instance.batch.show = false;
  let resolveSync;
  nextSync = new Promise(resolve => { resolveSync = resolve; });
  const syncing = instance.syncAccount(instance.rows[0], true);
  instance.page = 3; instance.selected = [1];
  instance.switchPool("key");
  assert.equal(instance.rows.length, 0, "category change must immediately clear stale rows");
  await V.nextTick(); await instance.reload();
  assert.equal(instance.page, 1);
  assert.equal(instance.selected.length, 0);
  assert.equal(instance.total, 1);
  assert.equal(instance.rows[0].id, 4);
  assert.equal(instance.channelOptions.length, 1);
  assert.equal(instance.typeOptions[0].value, "apikey");
  resolveSync({ ok: true, account: fixtures[0] });
  await syncing; await V.nextTick();
  assert.equal(instance.rows[0].id, 4, "late GPT sync must not reinsert an account into key pool");
  await instance.selectAllResults();
  assert.deepEqual(Array.from(instance.selected), [4]);
  app.unmount();
  console.log("Production Vue compile, GPT row fields, category scope, late sync, cross-page selection, preview, batch execution and details passed.");
})().catch(error => { console.error(error.name + ": " + error.message); process.exitCode = 1; });
