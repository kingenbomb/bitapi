/* Account management: interaction structure adapted from sub2api AccountsView.
   Uses bit-api's Vue/Naive UI shell and real Python account-management APIs. */
(function () {
  "use strict";
  const { h, ref, reactive, computed, watch, onMounted, onUnmounted } = Vue;
  const P = window.BitPortal;
  const { api, qs, msg, nf, usd, MONO, C, stack, tag, absTime, relTime, BarChart, ic, Ic, usageHint } = P;
  const { Load } = window.BitPortalPages;
  const naive = window.naive;
  const AdminIntro = window.BitPortalAdmin.AdminIntro;
  const { Settings: GPTSettings, Operations: GPTOperations, PAT: GPTPAT } = window.BitPortalGPT;
  const root = "/admin/pool";
  const TYPE = { oauth: "OAuth", apikey: "API Key", password: "邮箱密码", token: "Token / Cookie" };
  const ACTION = { pause: "停用调度", resume: "启用调度", reset: "重置为待检", recover: "恢复状态",
    refresh: "刷新凭据", test: "测试账号", delete: "删除账号", edit: "批量编辑", reset_quota: "重置额度", balance: "查询余额",
    usage: "刷新上游用量", privacy: "关闭上游训练", sync: "同步 GPT 账号" };
  const STATUS = { active: "可用", cooldown: "冷却", exhausted: "耗尽", dead: "失效", unchecked: "待检",
    paused: "已停用", temporary: "临时暂停", expired: "已到期", upstream: "上游限流" };
  const POOL_STATUS = {
    active: { label: "可用", type: "success" }, cooldown: { label: "冷却", type: "warning" },
    exhausted: { label: "耗尽", type: "warning" }, dead: { label: "失效", type: "error" },
    unchecked: { label: "待检", type: "default" }, paused: { label: "已停用", type: "info" },
  };
  const readPreference = (key, fallback) => {
    try { return JSON.parse(localStorage.getItem("bitapi-pool-" + key)) ?? fallback; }
    catch (_) { return fallback; }
  };
  const writePreference = (key, value) => localStorage.setItem("bitapi-pool-" + key, JSON.stringify(value));
  const compact = (n) => n >= 1e6 ? (n / 1e6).toFixed(2) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "K" : nf(n || 0);
  const clock = (v) => v ? absTime(v) : "—";
  async function imageTest(path, body, progress, controller) {
    let timer, expired = false, reader;
    const renew = () => { clearTimeout(timer); timer = setTimeout(() => { expired = true; controller.abort(); }, 150000); };
    renew();
    try {
      const response = await fetch(P.BASE + "/api" + path, { method: "POST", signal: controller.signal,
        headers: { "Content-Type": "application/json", ...P.authHeaders() }, credentials: "same-origin", body: JSON.stringify(body) });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        if (response.status === 401) { P.clearToken(); location.hash = "#/login"; }
        throw new Error(typeof data.detail === "string" ? data.detail : "生图测试请求失败（" + response.status + "）");
      }
      reader = response.body.getReader();
      const decoder = new TextDecoder();
      let pending = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        pending += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
        let end;
        while ((end = pending.indexOf("\n\n")) >= 0) {
          const frame = pending.slice(0, end); pending = pending.slice(end + 2);
          const lines = frame.split("\n"), kind = lines.find(line => line.startsWith("event:"))?.slice(6).trim();
          const data = lines.filter(line => line.startsWith("data:")).map(line => line.slice(5).trimStart()).join("\n");
          if (!data) continue; // Proxy keepalives do not extend the output-idle timer.
          const event = JSON.parse(data);
          if (kind === "progress") { renew(); progress(event.message); }
          if (kind === "result") return event;
        }
      }
      throw new Error("生图连接已结束，未收到测试结果，请重试");
    } catch (error) {
      if (expired) throw new Error("长时间未收到生图进度，请稍后重试");
      throw error;
    } finally {
      clearTimeout(timer);
      if (reader) reader.cancel().catch(() => {});
      controller.abort();
    }
  }
  const planLabel = (value) => {
    const key = String(value || "").toLowerCase().replace(/[\s_-]+/g, "");
    return { free: "Free", go: "Go", plus: "Plus", prolite: "Pro 100", pro: "Pro 200", promax: "Pro 500",
      team: "Business", selfservebusinessusagebased: "Business", business: "Enterprise",
      selfservebusinessprolite: "Business Premium", enterprise: "Enterprise", ent26: "Enterprise",
      enterprisecbpusagebased: "Enterprise", enterprisecbpautomation: "Enterprise (Automation)", edu: "Edu", eduplus: "Edu Plus", edupro: "Edu Pro" }[key] || value || "套餐未同步";
  };
  const blankForm = () => ({ channel: "", identity: "", name: "", account_type: "apikey", priority: 50,
    concurrency: 0, rate_multiplier: 1, group_ids: [], account_expires_at: 0, auto_pause_on_expired: true,
    proxy_id: null, quota_limit: 0, quota_daily_limit: 0, quota_weekly_limit: 0, base_rpm: 0, window_cost_limit: 0,
    paused: false, note: "", credentials: "" });

  const Pool = {
    components: { Load, AdminIntro, BarChart, Ic, GPTSettings, GPTOperations, GPTPAT, SiteUsage: P.LiveUsageMetrics },
    setup() {
      const rows = ref([]), total = ref(0), summary = ref({ totals: {}, channels: [] });
      const options = ref({ channels: [], groups: [], proxies: [] });
      const filters = reactive(Object.assign({ q: "", pool_kind: "account", channel: "gpt", account_type: null, status: null, group_id: null, schedulable: null, privacy_mode: null }, readPreference("filters-v2", {})));
      const page = ref(1), pageSize = ref(20), sort = reactive({ key: "id", order: "desc" });
      const selected = ref([]), allResults = ref(false), loading = ref(true), error = ref(""), updatedAt = ref(0);
      const busy = ref(""), autoSecs = ref(readPreference("auto", 0)), countdown = ref(autoSecs.value);
      const defaultHidden = ["id", "week", "groups", "proxy", "rate", "created", "note"];
      const hidden = ref(readPreference("hidden-v2-" + filters.pool_kind, defaultHidden));
      const syncing = reactive({}), syncErrors = reactive({}), now = ref(Math.floor(Date.now() / 1000));
      const syncQueue = [], syncPending = new Map();
      let syncActive = 0;
      const editor = reactive({ show: false, id: null, sourceId: null, form: blankForm(), error: "", saving: false });
      const patShow = ref(false), editKey = ref(0), resetRow = ref(null);
      const detail = reactive({ show: false, row: null, tab: "info", loading: false, error: "", stats: null, history: [], plan: null });
      const test = reactive({ show: false, row: null, model: null, kind: "chat", prompt: "画一只坐在窗边的橘猫，柔和自然光，简洁插画风格，不要文字。", result: null, running: false, elapsed: 0, progress: "", error: "" });
      let testTimer, testAbort;
      const planForm = reactive({ model: "", interval_seconds: 3600, enabled: false });
      const modelCatalog = reactive({}), modelLoading = reactive({}), modelPending = new Map(), modelTimers = new Map();
      const modelsFor = (row, kind = "chat") => row ? [
        ...(kind === "image" ? ((modelCatalog[row.id] || {}).image_models || []) : []),
        ...((modelCatalog[row.id] || {}).models || (row.channel === "gpt" ? [] : row.models.map((id) => ({ id, name: id }))))]
        .map((model) => ({ label: (model.backend === "web" ? model.name : model.id + (kind === "image" ? " · Codex 生图工具" : "")) + (model.hidden ? " · 隐藏" : "") +
          (model.test_status ? " · " + ({ verified: "已验证", busy: "上次繁忙", failed: "上次失败", untested: "未测试" }[model.test_status] || "未测试") : ""), value: model.id })) : [];
      const modelScan = (row) => (row && modelCatalog[row.id] || {}).scan || {};
      const loadTestModels = (row, detect = false, refresh = false) => {
        if (!row || row.channel !== "gpt") return Promise.resolve();
        if (modelPending.has(row.id)) return modelPending.get(row.id);
        if (!detect && !refresh && modelCatalog[row.id] && !modelScan(row).running) return Promise.resolve();
        clearTimeout(modelTimers.get(row.id));
        modelLoading[row.id] = true;
        const pending = api(detect ? "POST" : "GET", root + "/accounts/" + row.id + "/models" + (detect ? "/detect" : ""))
          .then((data) => {
            if (destroyed) return;
            modelCatalog[row.id] = data;
            const first = (data.models || [])[0];
            if (test.row && test.row.id === row.id && !test.model) test.model = (modelsFor(row, test.kind)[0] || {}).value || null;
            if (first && detail.row && detail.row.id === row.id && !planForm.model) planForm.model = first.id;
            if ((data.scan || {}).running) modelTimers.set(row.id, setTimeout(() => loadTestModels(row, false, true), 1500));
          })
          .catch((e) => { if (!destroyed) modelCatalog[row.id] = { ...modelCatalog[row.id], error: e.message }; })
          .finally(() => { modelLoading[row.id] = false; modelPending.delete(row.id); });
        modelPending.set(row.id, pending); return pending;
      };
      const modelHint = (row) => {
        if (!row || row.channel !== "gpt") return "使用此渠道配置的对话模型";
        const catalog = modelCatalog[row.id] || {};
        const scan = catalog.scan || {};
        if (scan.running) return "检测 " + scan.done + " / " + scan.total + (scan.current ? " · " + scan.current : " · 正在同步候选型号");
        if (catalog.error || scan.error) return catalog.error || scan.error;
        const count = (catalog.models || []).length, verified = catalog.verified_count ?? (catalog.models || []).filter((model) => model.test_status === "verified").length;
        return count ? "可选 " + count + " 个型号 · " + verified + " 个已验证。可选择或输入模型 ID 测试。" : "暂无模型目录，点击“检测可用模型”同步上游。";
      };
      const batch = reactive({ show: false, action: "", pool_kind: null, targets: [], running: false, error: "", results: [], done: 0, changes: {}, edit: false });
      const batchFields = reactive({ priority: false, concurrency: false, rate_multiplier: false, group_ids: false,
        proxy_id: false, account_expires_at: false, auto_pause_on_expired: false, quota_daily_limit: false, note: false });
      const batchForm = reactive(blankForm());
      const transfer = reactive({ show: false, content: "", contents: [], filenames: [], channel: null, update_existing: false, result: null, running: false, error: "" });
      const proxy = reactive({ show: false, name: "", url: "", running: false, error: "" });
      const proxyTests = reactive({});
      const pausing = reactive({ show: false, row: null, minutes: 60, reason: "" });
      const oauth = reactive({ show: false, row: null, proxy_id: null, session_id: "", auth_url: "", callback_url: "", running: false, error: "" });
      let requestSeq = 0, batchRequestSeq = 0, detailRequestSeq = 0, reloadTimer, autoTimer, destroyed = false;
      const params = () => Object.assign({}, filters, { sort: sort.key, order: sort.order,
        limit: pageSize.value, offset: (page.value - 1) * pageSize.value });
      const loadOptions = async () => { options.value = await api("GET", root + "/options"); };
      const reload = async (forceSync = false) => {
        const seq = ++requestSeq;
        loading.value = true; error.value = "";
        try {
          const [list, counts] = await Promise.all([api("GET", root + qs(params())), api("GET", root + "/summary" + qs(filters))]);
          if (seq !== requestSeq || destroyed) return;
          const maxPage = Math.max(1, Math.ceil(list.total / pageSize.value));
          if (page.value > maxPage) { page.value = maxPage; return; }
          rows.value = list.accounts; total.value = list.total; summary.value = counts;
          updatedAt.value = list.generated_at;
          queueVisibleSync(list.accounts, forceSync === true);
        } catch (e) { if (seq === requestSeq) error.value = e.message; }
        finally { if (seq === requestSeq) loading.value = false; }
      };
      const queueReload = (delay = 0) => { clearTimeout(reloadTimer); reloadTimer = setTimeout(reload, delay); };
      const clearSelection = () => { selected.value = []; allResults.value = false; };
      watch(() => JSON.stringify(filters), () => {
        page.value = 1; clearSelection(); writePreference("filters-v2", filters); queueReload(300);
      });
      watch([page, pageSize], () => queueReload());
      watch(() => JSON.stringify(sort), () => { page.value = 1; queueReload(); });
      watch(autoSecs, (v) => { countdown.value = v; writePreference("auto", v); });
      watch(hidden, (v) => writePreference("hidden-v2-" + filters.pool_kind, v), { deep: true });
      const modalOpen = computed(() => editor.show || detail.show || test.show || batch.show || transfer.show || proxy.show || pausing.show || oauth.show);
      watch(() => detail.tab, (tab) => { if (tab === "tests" && detail.show) loadTestModels(detail.row); });
      onMounted(async () => {
        await Promise.all([reload(), loadOptions().catch((e) => { error.value = e.message; })]);
        autoTimer = setInterval(() => {
          const currentTime = Math.floor(Date.now() / 1000);
          if (currentTime - now.value >= 60) now.value = currentTime;
          if (!autoSecs.value || loading.value || modalOpen.value || document.hidden) return;
          countdown.value -= 1;
          if (countdown.value <= 0) { countdown.value = autoSecs.value; reload(); }
        }, 1000);
      });
      onUnmounted(() => { destroyed = true; requestSeq++; clearTimeout(reloadTimer); clearInterval(autoTimer); clearInterval(testTimer); testAbort?.abort(); modelTimers.forEach(clearTimeout); });
      const channelOptions = computed(() => options.value.channels.filter((c) => !c.pool_kinds || c.pool_kinds.includes(filters.pool_kind))
        .map((c) => ({ label: c.name + (c.loaded ? "" : "（未加载）"), value: c.name })));
      const typeOptions = computed(() => Object.keys(TYPE).filter((key) => filters.pool_kind === "key" ? key === "apikey" : key !== "apikey")
        .map((key) => ({ label: TYPE[key], value: key })));
      const groupOptions = computed(() => options.value.groups.map((g) => ({ label: g.name, value: g.id })));
      const proxyOptions = computed(() => options.value.proxies.map((p) => ({ label: p.name + " · " + p.address, value: p.id, disabled: !p.enabled })));
      const groupNames = (ids) => ids.length ? ids.map((id) => (options.value.groups.find((g) => g.id === id) || {}).name || "#" + id).join("、") : "全部分组";
      const selectedChannel = computed(() => options.value.channels.find((c) => c.name === editor.form.channel));
      watch(() => editor.form.channel, (channel) => {
        if (!editor.id) editor.form.account_type = filters.pool_kind === "key" ? "apikey" : channel === "gpt" ? "oauth" : "token";
      });
      const counts = computed(() => summary.value.totals || {});
      const setFilterStatus = (s) => { filters.status = filters.status === s ? null : s; };
      const resetFilters = () => Object.keys(filters).forEach((k) => { if (k !== "pool_kind") filters[k] = k === "q" ? "" : null; });
      const switchPool = (kind) => {
        if (filters.pool_kind === kind) return;
        requestSeq++; rows.value = []; total.value = 0;
        resetFilters(); filters.pool_kind = kind; filters.channel = kind === "account" ? "gpt" : null;
        hidden.value = readPreference("hidden-v2-" + kind, defaultHidden);
        clearSelection(); page.value = 1;
      };
      const drainSync = () => {
        while (!destroyed && syncActive < 2 && syncQueue.length) {
          const job = syncQueue.shift(); syncActive++; syncing[job.id] = true;
          api("POST", root + "/accounts/" + job.id + "/sync", { force: job.force }).then((data) => {
            if (destroyed || !data.account) return data;
            const index = rows.value.findIndex((r) => r.id === job.id);
            if (index >= 0) rows.value[index] = data.account;
            if (detail.show && detail.row && detail.row.id === job.id) detail.row = data.account;
            syncErrors[job.id] = Object.values(data.errors || {}).join("；");
            return data;
          }).catch((e) => { if (!destroyed) syncErrors[job.id] = e.message; }).then((data) => {
            syncActive--; syncing[job.id] = false; syncPending.delete(job.id); job.resolve(data); drainSync();
          });
        }
        if (destroyed) while (syncQueue.length) { const job = syncQueue.shift(); syncPending.delete(job.id); job.resolve(); }
      };
      const syncAccount = (row, force = false) => {
        if (row.channel !== "gpt") return Promise.resolve();
        if (syncPending.has(row.id)) return syncPending.get(row.id);
        const promise = new Promise((resolve) => { syncQueue.push({ id: row.id, force, resolve }); });
        syncPending.set(row.id, promise); drainSync(); return promise;
      };
      const queueVisibleSync = (list, force = false) => {
        list.filter((r) => r.channel === "gpt").forEach((row) => {
          const snapshot = row.gpt_sync || {};
          const delay = Object.keys(snapshot.errors || {}).length ? 60 : 300;
          if (force || !snapshot.attempted_at || Date.now() / 1000 - snapshot.attempted_at >= delay) syncAccount(row, force);
        });
      };
      const syncMessage = (row) => syncErrors[row.id] || Object.values((row.gpt_sync || {}).errors || {}).join("；");
      const resetText = (value) => {
        if (!value) return "—";
        const seconds = Math.max(0, value - now.value);
        if (!seconds) return "到期";
        return seconds >= 86400 ? Math.ceil(seconds / 86400) + "d" : seconds >= 3600 ? Math.ceil(seconds / 3600) + "h" : Math.ceil(seconds / 60) + "m";
      };
      // 累计四指标。口径说明(2026-11-05 校准):
      //   Token    = input + output + cache_read + cache_write,分母含缓存;
      //              缓存单价通常只有输入价的 1/10,所以 Token 数会明显大于"按全价计费的输入"
      //   账号计费 = 官方原价 × 账号倍率
      //   用户计费 = 官方原价 × 分组倍率
      // 两者乘的是**不同倍率**,不能相减当毛利。百分比一律对官方原价,那才是同一把尺子。
      // 有上游账号归属才谈得上成本。后端 costed = 记了 account_cost 的行数。
      const usedAccountCost = (r) => (r.total_usage || {}).costed > 0;
      const gptTotal = (r) => {
        const u = r.total_usage || {};
        const std = u.standard_cost || 0;
        const pct = (v) => std > 0 ? " (" + (v / std * 100).toFixed(1) + "%)" : "";
        return h("div", { class: "pool-gpt-metrics", role: "group", "aria-label": "GPT 累计用量" }, [
          ["response", "请求", nf(u.requests), "requests",
           "累计请求 " + nf(u.requests)],
          ["database", "Token（含缓存读写）", compact(u.tokens), "tokens",
           "累计Token " + nf(u.tokens) + " · 含缓存读写,缓存单价通常仅为输入价的 1/10"],
          ["wallet", "账号计费", usd(u.cost) + pct(u.cost), "account",
           "累计账号计费 " + usd(u.cost) + pct(u.cost) + " · 官方原价 " + usd(std) + " × 账号倍率"],
          ["user", "用户计费", usd(u.user_cost) + pct(u.user_cost), "user",
           "累计用户计费 " + usd(u.user_cost) + pct(u.user_cost) + " · 官方原价 " + usd(std) + " × 分组倍率"],
        ].map(([icon, label, value, tone, tip]) => usageHint(tip,
          [ic(icon, 16), h("b", value)], { class: "pool-gpt-metric pool-gpt-metric-" + tone })));
      };
      const gptWindow = (r, key) => {
        const window = (r.upstream_usage || {})[key], label = key === "five_hour" ? "5h" : "7d";
        const value = window && window.utilization;
        const stale = !!(window && window.reset_at && window.reset_at <= now.value);
        const percent = stale ? "↻" : value == null ? "—" : Math.round(value * 10) / 10 + "%";
        const tone = stale || value == null ? "unknown" : value >= 90 ? "danger" : value >= 75 ? "warning" : key;
        const stats = (r.usage_windows || {})[key] || (window && window.window_stats);
        const description = label + " 窗口：" + (stale ? "已到重置时间，等待同步" : value == null ? "上游未提供用量" : percent + " 已用") +
          "；" + (window && window.reset_at ? "重置于 " + clock(window.reset_at) : "重置时间未知");
        return usageHint(description, [
          h("span", { class: "pool-window-label " + key }, label),
          h("span", { class: "pool-quota-track" + (tone === "unknown" ? " is-unknown" : "") }, [
            h("i", { class: tone, style: { width: value == null || stale ? "0%" : Math.min(100, Math.max(0, value)) + "%" } })]),
          h("b", { class: "pool-window-percent " + tone }, percent),
          h("span", { class: "pool-window-reset" }, [ic("clock", 12), resetText(window && window.reset_at)]),
        ], { class: "pool-gpt-window-hint", tip: [h("div", description), stats && (stats.requests || stats.tokens || stats.cost || stats.user_cost) ? h("div", { class: "pool-window-tip-stats" }, [
          h("span", "本站已记录请求 " + nf(stats.requests) + " · Token " + nf(stats.tokens)),
          h("span", "账号计费 " + (stats.costed > 0 ? usd(stats.cost) : "—") +
            " · 用户计费 " + usd(stats.user_cost) + " · 官方原价 " + usd(stats.standard_cost)),
        ]) : null] });
      };
      const creditsValue = (r) => {
        const credits = (r.upstream_usage || {}).credits;
        return !credits ? "—" : credits.unlimited === true ? "∞" : credits.has_credits === false ? "0" : credits.balance == null ? "—" : credits.balance;
      };
      const gptQuota = (r) => {
        const message = syncMessage(r), sync = r.gpt_sync || {}, pending = !!syncing[r.id], usage = r.upstream_usage || {};
        const status = pending ? "同步中" : message ? (sync.updated_at ? "部分同步" : "同步失败") : sync.updated_at ? relTime(sync.updated_at) : "未同步";
        const tone = pending ? "pending" : message ? (sync.updated_at ? "warning" : "danger") : sync.updated_at ? "success" : "unknown";
        const description = message || (sync.updated_at ? "上游数据更新于 " + clock(sync.updated_at) : "尚未同步 GPT 上游资料与用量");
        const windows = ["five_hour", "seven_day"].filter((key) => usage[key] && usage[key].utilization != null);
        const missing = ["five_hour", "seven_day"].filter((key) => !windows.includes(key)).map((key) => key === "five_hour" ? "5h" : "7d");
        const resets = r.gpt_reset_credits;
        const expires = resets && (resets.expirations || []).map(clock).join("；");
        const canReset = !r.parent_account_id && resets && resets.available_count > 0;
        return h("div", { class: "pool-quota-cell pool-gpt-quota" }, [
          ...windows.map((key) => gptWindow(r, key)),
          h("div", { class: "pool-quota-badges" }, [
            usageHint("Codex 积分余额：" + creditsValue(r) + "（积分，与本站费用和额度重置券分别计算）",
              [ic("coins", 13), h("b", creditsValue(r))], { class: "pool-quota-badge credits" }),
            usageHint("可用额度重置券：" + (resets ? resets.available_count + " 张" : "尚未查询") + (expires ? "；到期：" + expires : "") + (canReset ? "；点击使用 1 张重置券" : ""),
              [ic("gift", 13), h("b", resets ? String(resets.available_count) : "—")], {
                class: "pool-quota-badge resets", activate: canReset ? () => { resetRow.value = r; } : undefined }),
            typeof usage.allowed === "boolean" ? usageHint(usage.limit_reached ? "上游报告额度已用尽" : usage.allowed ? "上游额度允许请求" : "上游额度暂不允许请求",
              [ic(usage.allowed && !usage.limit_reached ? "circleCheck" : "alert", 13)],
              { class: "pool-quota-badge " + (usage.allowed && !usage.limit_reached ? "allowed" : "limited") }) : null,
            missing.length ? usageHint("本账号上游未返回 " + missing.join(" / ") + " 用量，未绘制对应进度条；未知不代表用量为 0 或已耗尽。",
              [h("span", missing.join("/") + " —")], { class: "pool-quota-badge missing" }) : null,
          ]),
          h("div", { class: "pool-quota-actions" }, [
            usageHint(description, [ic(pending ? "clock" : message ? "alert" : sync.updated_at ? "circleCheck" : "clock", 13), h("span", status)],
              { class: "pool-sync-status " + tone }),
            h(naive.NButton, { size: "tiny", quaternary: true, circle: true, loading: pending,
              "aria-label": "同步 " + r.name + " 的 GPT 资料与用量", title: "同步 GPT 资料与用量",
              onClick: () => syncAccount(r, true) }, () => ic("refresh", 15)),
          ]),
        ]);
      };
      const changeColumn = (key, checked) => { hidden.value = checked ? hidden.value.filter((k) => k !== key) : hidden.value.concat(key); };
      const updateSort = (s) => { sort.key = s && s.order ? s.columnKey : "id"; sort.order = s && s.order === "ascend" ? "asc" : "desc"; };
      const previewTargets = async (body) => {
        const targets = [];
        let offset = 0;
        do {
          const page = await api("POST", root + "/preview", { ...body, offset });
          targets.push(...page.targets);
          offset = page.next_offset == null ? null : page.next_offset;
        } while (offset !== null);
        return { targets };
      };
      const selectAllResults = async () => {
        busy.value = "select";
        const snapshot = JSON.stringify(filters);
        try {
          const r = await previewTargets({ filters: { ...filters } });
          if (snapshot !== JSON.stringify(filters) || destroyed) return;
          selected.value = r.targets.map((x) => x.id); allResults.value = true;
        }
        catch (e) { msg.error(e.message); } finally { busy.value = ""; }
      };
      const openEditor = (row) => {
        editor.id = row ? row.id : null; editor.sourceId = null; editor.error = "";
        editor.form = Object.assign(blankForm(), row || {}, { credentials: "" });
        editor.form.gpt_settings = JSON.parse(JSON.stringify(row?.gpt_settings || {})); editKey.value++;
        if (!row) { editor.form.channel = filters.pool_kind === "account" ? "gpt" : filters.channel || ""; editor.form.account_type = filters.pool_kind === "account" ? "oauth" : "apikey"; }
        editor.show = true;
      };
      const saveEditor = async () => {
        editor.saving = true; editor.error = "";
        try {
          const body = {};
          if (editor.form.channel === "gpt" && editor.id) body.gpt_settings = editor.form.gpt_settings;
          Object.keys(blankForm()).forEach((k) => { if (!["credentials", "channel", "identity"].includes(k)) body[k] = editor.form[k]; });
          if (editor.form.credentials.trim()) {
            const text = editor.form.credentials.trim();
            body.credentials = text.startsWith("{") ? JSON.parse(text) : { [editor.form.channel === "gpt" ? "access_token" : "api_key"]: text };
          }
          if (!editor.id) {
            if (!editor.sourceId) body.channel = editor.form.channel;
            body.identity = editor.form.identity.trim();
            if (!body.name.trim()) body.name = body.identity;
            body.pool_kind = filters.pool_kind;
          }
          const path = editor.sourceId ? "/accounts/" + editor.sourceId + "/duplicate" : "/accounts" + (editor.id ? "/" + editor.id : "");
          await api(editor.id ? "PATCH" : "POST", root + path, body);
          editor.show = false; msg.success("账号已保存"); await reload();
          if (detail.show && detail.row?.id === editor.id) detail.row = (await api("GET", root + "/accounts/" + editor.id)).account;
        } catch (e) { editor.error = e.message; } finally { editor.saving = false; }
      };
      const setPaused = async (row, paused) => {
        busy.value = "row" + row.id;
        try { await api("PATCH", root + "/accounts/" + row.id, { paused }); await reload(); }
        catch (e) { msg.error(e.message); } finally { busy.value = ""; }
      };
      const openBatch = async (action, ids = null, filtered = false) => {
        const seq = ++batchRequestSeq;
        batch.action = action; batch.targets = []; batch.results = []; batch.done = 0; batch.error = ""; batch.edit = action === "edit";
        batch.pool_kind = filters.pool_kind;
        batch.show = true; busy.value = "preview";
        try {
          const body = filtered ? { filters: { ...filters } } : { ids: ids || selected.value.slice(), filters: { pool_kind: batch.pool_kind } };
          const result = await previewTargets(body);
          if (seq === batchRequestSeq && batch.show) batch.targets = result.targets;
        } catch (e) { if (seq === batchRequestSeq) batch.error = e.message; }
        finally { if (seq === batchRequestSeq) busy.value = ""; }
      };
      const runBatch = async () => {
        batch.running = true; batch.error = ""; batch.results = []; batch.done = 0;
        try {
          const changes = {};
          if (batch.action === "edit") Object.keys(batchFields).filter((k) => batchFields[k]).forEach((k) => { changes[k] = batchForm[k]; });
          if (batch.action === "edit" && !Object.keys(changes).length) throw new Error("请选择至少一个要修改的字段");
          const chunkSize = ["sync", "usage", "refresh", "test", "privacy", "balance"].includes(batch.action) ? 1 : 100;
          for (let start = 0; start < batch.targets.length; start += chunkSize) {
            const part = batch.targets.slice(start, start + chunkSize), versions = {};
            part.forEach((r) => { versions[r.id] = r.version; });
            const r = await api("POST", root + "/bulk", { action: batch.action, pool_kind: batch.pool_kind, ids: part.map((x) => x.id), versions, changes });
            batch.results.push(...r.results); batch.done += part.length;
          }
          selected.value = batch.results.filter((r) => !r.ok).map((r) => r.id); allResults.value = false;
          await reload();
        } catch (e) {
          batch.error = e.message;
          const succeeded = new Set(batch.results.filter((r) => r.ok).map((r) => r.id));
          selected.value = batch.targets.filter((r) => !succeeded.has(r.id)).map((r) => r.id);
          allResults.value = false;
          await reload();
        } finally { batch.running = false; }
      };
      const openDetails = async (row, tab = "info") => {
        const seq = ++detailRequestSeq;
        detail.row = row; detail.tab = tab; detail.show = true; detail.stats = null; detail.history = []; detail.error = ""; detail.loading = true;
        if (tab === "tests") loadTestModels(row);
        const aid = row.id;
        try {
          const [a, s, t] = await Promise.all([api("GET", root + "/accounts/" + aid), api("GET", root + "/accounts/" + aid + "/stats"), api("GET", root + "/accounts/" + aid + "/tests")]);
          if (!detail.show || detail.row.id !== aid || seq !== detailRequestSeq) return;
          detail.row = a.account; detail.stats = s; detail.history = t.history; detail.plan = t.plan;
          Object.assign(planForm, { model: t.plan ? t.plan.model : ((modelsFor(a.account)[0] || {}).value || ""), interval_seconds: t.plan ? t.plan.interval_seconds : 3600, enabled: !!(t.plan && t.plan.enabled) });
        } catch (e) { if (seq === detailRequestSeq) detail.error = e.message; }
        finally { if (seq === detailRequestSeq) detail.loading = false; }
      };
      const openTest = (row, model = null) => {
        if (test.running) return;
        Object.assign(test, { show: true, row, kind: "chat", model: model || (modelsFor(row)[0] || {}).value || null, result: null, error: "" });
        loadTestModels(row);
      };
      const setTestKind = (kind) => {
        if (test.running) return;
        test.kind = kind; test.result = null; test.error = "";
        test.model = (modelsFor(test.row, kind)[0] || {}).value || null;
      };
      const closeTestOnEsc = () => {
        // Image preview handles its own Escape; retain the generated result.
        if (!test.running && !document.querySelector(".n-image-preview-container")) test.show = false;
      };
      const runTest = async () => {
        if (test.running) return;
        if (test.kind === "image" && !test.prompt.trim()) { test.error = "请输入生图描述"; return; }
        test.running = true; test.result = null; test.error = ""; test.elapsed = 0;
        const started = Date.now(), row = test.row, aid = row.id;
        test.progress = "正在准备生图";
        testTimer = setInterval(() => { test.elapsed = Math.floor((Date.now() - started) / 1000); }, 1000);
        try {
          if (test.kind === "image" && row.channel === "gpt") {
            testAbort = new AbortController();
            test.result = await imageTest(root + "/accounts/" + aid + "/test-image", { model: test.model, prompt: test.prompt.trim() },
              message => { test.progress = message; }, testAbort);
          } else {
            test.result = await api("POST", root + "/accounts/" + aid + "/test", { model: test.model }, { timeout: row.channel === "gpt" ? 35000 : 0 });
          }
          if (!test.result || typeof test.result.ok !== "boolean") throw new Error("测试接口未返回有效结果，请稍后重试");
        }
        catch (e) { test.error = e.message; }
        finally { test.running = false; clearInterval(testTimer); testAbort = null; }
        if (destroyed) return;
        // Refreshes must not hold the test button or hide a completed answer.
        loadTestModels(row, false, true);
        reload();
        try {
          if (detail.show && detail.row.id === aid) {
            const history = await api("GET", root + "/accounts/" + aid + "/tests");
            if (detail.show && detail.row.id === aid) detail.history = history.history;
          }
        }
        catch (e) { if (detail.show && detail.row.id === aid) detail.error = e.message; }
      };
      const savePlan = async () => {
        busy.value = "plan";
        try { await api("PUT", root + "/accounts/" + detail.row.id + "/plan", { ...planForm }); msg.success("测试计划已保存"); await openDetails(detail.row, "tests"); }
        catch (e) { detail.error = e.message; } finally { busy.value = ""; }
      };
      const runAction = async (row, action) => {
        busy.value = "row" + row.id;
        try { await api("POST", root + "/accounts/" + row.id + "/action/" + action); msg.success(ACTION[action] + "完成"); await reload(); }
        catch (e) { msg.error(e.message); } finally { busy.value = ""; }
      };
      const rowAction = (key, row) => {
        if (key === "sync") return syncAccount(row, true);
        if (key === "duplicate") {
          openEditor(row); editor.sourceId = row.id; editor.id = null;
          editor.form.identity = ""; editor.form.name = row.name + "（副本）";
          return;
        }
        if (key === "oauth") return openOAuth(row);
        if (key === "edit") return openEditor(row);
        if (key === "stats") return openDetails(row, "stats");
        if (key === "test") return openTest(row);
        if (key === "plan") return openDetails(row, "tests");
        if (key === "gpt") return openDetails(row, "gpt");
        if (key === "pause_temp") { Object.assign(pausing, { show: true, row, minutes: 60, reason: "" }); return; }
        if (key === "delete") return openBatch("delete", [row.id]);
        runAction(row, key);
      };
      const saveTemporary = async () => {
        if (!Number.isInteger(pausing.minutes) || pausing.minutes < 1) return msg.error("请输入有效的暂停分钟数");
        busy.value = "temporary";
        try { await api("PATCH", root + "/accounts/" + pausing.row.id, { temp_until: Math.floor(Date.now() / 1000) + pausing.minutes * 60, temp_reason: pausing.reason }); pausing.show = false; await reload(); }
        catch (e) { msg.error(e.message); } finally { busy.value = ""; }
      };
      const previewImport = async (apply = false) => {
        transfer.running = true; transfer.error = "";
        try {
          transfer.result = await api("POST", root + (apply ? "/import" : "/import/preview"), { content: transfer.content, contents: transfer.contents, channel: transfer.channel, update_existing: transfer.update_existing, pool_kind: filters.pool_kind });
          if (apply) {
            const result = transfer.result;
            result.applied = true;
            const summary = "新增 " + result.imported + " · 更新 " + result.updated + " · 跳过 " + result.skipped + " · 失败 " + result.failed;
            if (!result.failed) {
              transfer.show = false;
              Object.assign(transfer, { content: "", contents: [], filenames: [], result: null });
              msg.success("导入完成：" + summary);
            } else {
              msg.warning("部分导入失败：" + summary + "，请查看失败明细");
            }
            await Promise.all([reload(), loadOptions()]);
          }
        } catch (e) { transfer.error = e.message; } finally { transfer.running = false; }
      };
      watch(() => [transfer.content, transfer.contents, transfer.channel, transfer.update_existing], () => { transfer.result = null; });
      const readFiles = async (event) => {
        const files = Array.from(event.target.files || []); event.target.value = "";
        if (files.reduce((n, f) => n + f.size, 0) > 10 * 1024 * 1024) return (transfer.error = "文件合计不能超过 10 MB");
        transfer.running = true;
        try { transfer.contents = await Promise.all(files.map((f) => f.text())); transfer.filenames = files.map((f) => f.name); }
        catch (e) { transfer.error = "读取文件失败：" + e.message; }
        finally { transfer.running = false; }
      };
      const exportAccounts = async () => {
        busy.value = "export";
        try {
          const body = selected.value.length ? { ids: selected.value.slice(), filters: { pool_kind: filters.pool_kind } } : { filters: { ...filters } };
          const data = await api("POST", root + "/export", body);
          const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
          const a = document.createElement("a"); a.href = url; a.download = "bit-api-accounts.json"; a.click();
          setTimeout(() => URL.revokeObjectURL(url), 1000); msg.success("已导出 " + data.accounts.length + " 个账号");
        } catch (e) { msg.error(e.message); } finally { busy.value = ""; }
      };
      const saveProxy = async () => {
        proxy.running = true; proxy.error = "";
        try { await api("POST", root + "/proxies", { name: proxy.name.trim(), url: proxy.url }); proxy.name = ""; proxy.url = ""; await loadOptions(); }
        catch (e) { proxy.error = e.message; } finally { proxy.running = false; }
      };
      const testProxy = async (id) => {
        if (proxyTests[id] && proxyTests[id].running) return;
        proxyTests[id] = { running: true };
        try { proxyTests[id] = { ...await api("POST", root + "/proxies/" + id + "/test", {}, { timeout: 15000 }), running: false }; }
        catch (e) { proxyTests[id] = { running: false, ok: false,
          message: e.message.includes("测试请求等待超时") ? "代理测试请求超时，请稍后重试" : e.message }; }
      };
      const removeProxy = async (id) => {
        try { await api("DELETE", root + "/proxies/" + id); await loadOptions(); }
        catch (e) { proxy.error = e.message; }
      };
      const canTest = (r) => r.capabilities.includes("chat") && (r.channel === "gpt" || r.models.length > 0);
      const openOAuth = (row = null) => {
        Object.assign(oauth, { show: true, row, proxy_id: row ? row.proxy_id : null, session_id: "", auth_url: "", callback_url: "", error: "" });
      };
      const startOAuth = async () => {
        oauth.running = true; oauth.error = "";
        try {
          const data = await api("POST", root + "/oauth/gpt/start", { account_id: oauth.row ? oauth.row.id : null, proxy_id: oauth.proxy_id });
          oauth.session_id = data.session_id; oauth.auth_url = data.auth_url;
        } catch (e) { oauth.error = e.message; } finally { oauth.running = false; }
      };
      const finishOAuth = async () => {
        oauth.running = true; oauth.error = "";
        try {
          const data = await api("POST", root + "/oauth/gpt/finish", { session_id: oauth.session_id, callback_url: oauth.callback_url });
          oauth.show = false; oauth.callback_url = "";
          msg.success(data.action === "skipped" ? "此账号已存在" : "授权凭据已保存"); await reload();
        } catch (e) { oauth.error = e.message; } finally { oauth.running = false; }
      };
      const menuOptions = (r) => [
        { key: "gpt", label: "重置券 / 邀请 / Spark", disabled: r.channel !== "gpt" },
        { key: "sync", label: "同步 GPT 账号资料与用量", disabled: r.channel !== "gpt" },
        { key: "duplicate", label: "复制账号", disabled: r.account_type !== "apikey" },
        { key: "stats", label: "用量统计" }, { key: "plan", label: "定时测试", disabled: !canTest(r) },
        { key: "refresh", label: "刷新凭据", disabled: !r.capabilities.includes("refresh_token") },
        { key: "oauth", label: "重新 OAuth 授权", disabled: r.channel !== "gpt" || !!r.parent_account_id || r.auth_mode==='agentIdentity' },
        { key: "recover", label: "恢复状态 / 清除临时暂停" }, { key: "balance", label: "查询余额", disabled: !r.capabilities.includes("balance") },
        { key: "usage", label: "刷新上游用量窗口", disabled: !r.can_usage },
        { key: "privacy", label: "关闭上游模型训练", disabled: !r.can_privacy },
        { key: "pause_temp", label: "临时暂停调度" }, { key: "reset_quota", label: "重置本站额度" },
        { type: "divider", key: "divider" }, { key: "delete", label: "删除账号" },
      ];
      const allColumns = computed(() => [
        { type: "selection", key: "select", width: 38, fixed: "left" },
        { title: "账号名称", key: "name", width: 220, fixed: "left", sorter: true, render: (r) =>
          h("button", { class: "pool-account-name", onClick: () => openDetails(r), title: r.identity }, [
            h("strong", r.name), r.email && r.email !== r.name ? h("span", r.email) : null,
            h("span", "#" + r.id)]) },
        { title: "ID", key: "id", width: 70, sorter: true },
        { title: "账号类型", key: "platform", width: 150, render: (r) => h("div", { class: "pool-platform-cell" }, [
          h("div", { class: "pool-platform-badges" }, [h("strong", r.channel === "gpt" ? "OpenAI" : r.channel),
            h("span", { class: "pool-auth-badge" }, r.parent_account_id ? "Spark" : r.auth_mode==='personal_access_token'?'PAT':r.auth_mode==='agentIdentity'?'Agent':TYPE[r.account_type] || r.account_type)]),
          r.channel === "gpt" ? h("span", { class: "pool-plan-badge", title: "订阅到期：" + clock(r.subscription_expires_at) }, planLabel(r.plan_type)) : null,
          r.privacy_mode === "training_off" ? h("span", { class: "pool-privacy-badge" }, "Privacy") : null]) },
        { title: "容量", key: "concurrency", width: 115, sorter: true, render: (r) => stack(h("span", { style: MONO }, "并发 " + r.inflight + " / " + (r.concurrency || "∞")), "RPM " + (r.current_rpm || 0) + " / " + (r.base_rpm || "∞")) },
        { title: "状态", key: "status", width: 114, sorter: true, render: (r) => h("div", { title: r.last_error || r.temp_reason || r.status_label }, [
          tag(r.paused ? "info" : r.expired || r.status === "dead" ? "error" : r.upstream_until > Date.now()/1000 || r.temp_until > Date.now() / 1000 ? "warning" : (POOL_STATUS[r.status] || {}).type || "default", r.status_label),
          r.last_error ? h("div", { class: ["pool-error-snippet", { "is-capacity": r.last_error_kind === "capacity" }] }, r.last_error_kind === "capacity" ? "上游繁忙" : r.last_error) : null]) },
        { title: "调度", key: "schedulable", width: 76, render: (r) => h(naive.NSwitch, { size: "small", value: !r.paused,
          loading: busy.value === "row" + r.id, onUpdateValue: (v) => setPaused(r, !v), "aria-label": r.name + "的调度开关" }) },
        { title: "累计用量", key: "total_usage", width: 190, render: (r) => r.channel === "gpt" ? gptTotal(r) : h("div", { class: "pool-usage-cell" }, [
          h("span", [h("small", "请求"), h("b", nf(r.total_usage.requests))]), h("span", [h("small", "Token"), h("b", compact(r.total_usage.tokens))]),
          // 账号计费只在拿得到上游账号归属时才有值,否则后端写 NULL → 这里显示 "—" 而不是 $0.0000,
          // 否则会让人以为"这条路真的零成本",而实际是全站 95% 的请求都没记成本。
          usedAccountCost(r) ? h("span", [h("small", "账号计费"), h("b", usd(r.total_usage.cost))])
            : h("span", { title: "本渠道记录里没有上游账号归属,成本无从计算;官方原价为 " + usd(r.total_usage.standard_cost) },
                [h("small", "账号计费"), h("b", "—")]),
          h("span", [h("small", "用户计费"), h("b", usd(r.total_usage.user_cost))])]) },
        { title: "近 7 天", key: "week", width: 130, render: (r) => stack(nf(r.week.requests) + " 次 · " + compact(r.week.tokens), usd(r.week.cost)) },
        { title: "用量窗口", key: "usage", width: 250, render: (r) => {
          if (!r.can_usage) return h("span", { class: "pool-stat-note" }, "上游未提供");
          return gptQuota(r);
        } },
        { title: "分组", key: "groups", width: 140, render: (r) => h("span", { title: groupNames(r.group_ids || []) }, groupNames(r.group_ids || [])) },
        { title: "代理", key: "proxy", width: 130, render: (r) => (options.value.proxies.find((p) => p.id === r.proxy_id) || {}).name || "直连" },
        { title: "优先级", key: "priority", width: 88, sorter: true, render: (r) => h("span", { style: MONO, title: "数字越小优先级越高" }, r.priority) },
        { title: "账号倍率", key: "rate", width: 96, render: (r) => r.rate_multiplier + "×" },
        { title: "最后使用", key: "last_used_at", width: 120, sorter: true, render: (r) => h("span", { title: clock(r.last_used_at) }, r.last_used_at ? relTime(r.last_used_at) : "尚未记录") },
        { title: "到期时间", key: "account_expires_at", width: 166, sorter: true, render: (r) => h("div", { class: "pool-expiry-cell" }, [
          h("span", r.account_expires_at ? clock(r.account_expires_at) : "账号未设到期"),
          r.subscription_expires_at ? h("small", "订阅：" + clock(r.subscription_expires_at)) : null,
          h("small", r.expires_at ? "凭据：" + clock(r.expires_at) : "凭据未提供到期")]) },
        { title: "创建时间", key: "created", width: 150, render: (r) => clock(r.created_at) },
        { title: "备注", key: "note", width: 170, ellipsis: { tooltip: true }, render: (r) => r.note || "—" },
        { title: "操作", key: "actions", width: 164, fixed: "right", render: (r) => h(naive.NSpace, { size: 5, wrap: false }, () => [
          h(naive.NButton, { size: "tiny", secondary: true, onClick: () => openEditor(r) }, () => "编辑"),
          h(naive.NButton, { size: "tiny", secondary: true, disabled: !canTest(r), onClick: () => openTest(r) }, () => "测试"),
          h(naive.NDropdown, { trigger: "click", options: menuOptions(r), onSelect: (key) => rowAction(key, r) }, () =>
            h(naive.NButton, { size: "tiny", quaternary: true, "aria-label": r.name + "的更多操作" }, () => "•••"))]) },
      ]);
      const columns = computed(() => allColumns.value.filter((c) => c.key === "platform" || !hidden.value.includes(c.key)).map((c) => Object.assign({}, c,
        c.sorter ? { sortOrder: sort.key === c.key ? sort.order === "asc" ? "ascend" : "descend" : false } : {})));
      const columnOptions = computed(() => allColumns.value.filter((c) => !["select", "name", "platform", "actions"].includes(c.key)));
      const scrollWidth = computed(() => columns.value.reduce((n, c) => n + (c.width || 120), 0));
      const chartItems = computed(() => ((detail.stats || {}).series || []).map((r) => ({ label: r.day, value: r.requests })));
      const hasStats = computed(() => !!(detail.stats && detail.stats.summary.requests));
      const detailSections = (r) => [
        { title: "账号资料", icon: "user", fields: [
          ["账号标识", r.identity, true], ["认证方式", TYPE[r.account_type]],
          [r.channel === "gpt" ? "工作区" : "上游余额", r.channel === "gpt" ? r.workspace_name || "—" : r.balance == null ? "上游未提供" : String(r.balance)], ["备注", r.note || "未填写", true]] },
        { title: "调度设置", icon: "sliders", fields: [
          ["优先级", r.priority + " · 数值越小越优先"], ["当前并发 / 上限", r.inflight + " / " + (r.concurrency || "不限")],
          ["当前 RPM / 上限", (r.current_rpm || 0) + " / " + (r.base_rpm || "不限")], ["计费倍率", r.rate_multiplier + "×"],
          ["用户分组", groupNames(r.group_ids || [])], ["账号代理", (options.value.proxies.find((p) => p.id === r.proxy_id) || {}).name || "直连"]] },
        { title: "有效期与活动", icon: "clock", fields: [
          ["账号到期", r.account_expires_at ? clock(r.account_expires_at) : "未设置"], ["凭据到期", clock(r.expires_at)],
          ["创建时间", clock(r.created_at)], ["最后使用", r.last_used_at ? clock(r.last_used_at) : "尚未记录"],
          ["最近检查", clock(r.last_check)], ["订阅到期", clock(r.subscription_expires_at)]] },
      ];
      const quotaWindows = (r) => {
        const usage = r.upstream_usage || {}, items = [];
        const append = (value, prefix = "") => ["five_hour", "seven_day"].forEach((key) => {
          const window = value && value[key];
          if (window) items.push({ label: prefix + (key === "five_hour" ? "5h" : "7d"),
            value: window.utilization, width: Math.max(0, Math.min(100, window.utilization || 0)),
            reset: window.reset_at, tone: window.utilization >= 90 ? "danger" : window.utilization >= 75 ? "warning" : key });
        });
        append(usage); append(usage.code_review, "代码审查 · ");
        (usage.additional_limits || []).forEach((limit) => append(limit, limit.name + " · "));
        return items;
      };
      const failedResults = computed(() => batch.results.filter((r) => !r.ok));
      const expiryMillis = computed({ get: () => editor.form.account_expires_at ? editor.form.account_expires_at * 1000 : null,
        set: (v) => { editor.form.account_expires_at = v ? Math.floor(v / 1000) : 0; } });
      return { rows, total, summary, filters, page, pageSize, sort, selected, allResults, loading, error, updatedAt, busy,
        patShow, editKey, resetRow,
        autoSecs, countdown, hidden, options, editor, detail, test, planForm, batch, batchFields, batchForm, transfer, proxy, pausing,
        channelOptions, typeOptions, groupOptions, proxyOptions, selectedChannel, counts, columns, columnOptions, scrollWidth, chartItems,
        failedResults, expiryMillis, TYPE, STATUS, ACTION, POOL_STATUS, nf, usd, compact, clock, relTime, groupNames, canTest,
        syncing, syncErrors, switchPool, syncAccount, queueVisibleSync, planLabel, creditsValue,
        modelCatalog, modelLoading, modelScan, modelsFor, loadTestModels, modelHint, detailSections, quotaWindows, hasStats,
        reload, clearSelection, selectAllResults, setFilterStatus, resetFilters, changeColumn, updateSort, openEditor, saveEditor,
        openBatch, runBatch, openDetails, openTest, setTestKind, closeTestOnEsc, runTest, savePlan, saveTemporary, previewImport, readFiles, exportAccounts,
        saveProxy, removeProxy, testProxy, proxyTests, setPaused, runAction, oauth, openOAuth, startOAuth, finishOAuth };
    },
    template: `
<div class="pool-manager">
  <AdminIntro :title="filters.pool_kind==='account' ? '账号池' : 'Key 池'" :note="filters.pool_kind==='account' ? '一号一条记录，GPT 账号资料与用量直接从上游同步。' : '管理 API Key 库存、状态与调度。统计只归属实际调用的 Key。'">
    <n-button secondary :loading="loading" @click="reload(true)">刷新</n-button>
    <n-button v-if="filters.pool_kind==='account'" secondary @click="queueVisibleSync(rows,true)">同步当前页 GPT 账号</n-button>
    <n-button secondary @click="transfer.show=true">导入账号</n-button>
    <n-popconfirm @positive-click="exportAccounts"><template #trigger><n-button secondary :loading="busy==='export'">导出</n-button></template>
      导出{{ selected.length ? '选中的 '+selected.length+' 个账号' : '当前筛选的全部账号' }}，文件包含访问凭据，请妥善保管。
    </n-popconfirm>
    <n-button secondary @click="proxy.show=true">代理</n-button>
    <n-button v-if="filters.pool_kind==='account'" secondary @click="openOAuth()">GPT OAuth 授权</n-button>
    <n-button v-if="filters.pool_kind==='account'" secondary @click="patShow=true">Codex PAT</n-button>
    <n-button type="primary" @click="openEditor(null)">新建账号</n-button>
  </AdminIntro>
  <div class="pool-category-tabs" role="tablist" aria-label="池分类">
    <button v-for="kind in ['account','key']" :key="kind" role="tab" :aria-selected="filters.pool_kind===kind" :class="{active:filters.pool_kind===kind}" @click="switchPool(kind)">
      <strong>{{ kind==='account' ? '账号池' : 'Key 池' }}</strong><span>{{ summary.categories ? nf(summary.categories[kind]) : '—' }}</span>
      <small>{{ kind==='account' ? 'OAuth · 密码 · 会话账号' : 'API Key 凭据' }}</small>
    </button>
  </div>
  <div class="pool-status-strip">
    <button :class="{active:!filters.status}" @click="filters.status=null"><span>全部账号</span><strong>{{ nf(counts.total) }}</strong></button>
    <button v-for="s in ['active','cooldown','exhausted','dead','paused']" :key="s" :class="{active:filters.status===s}" @click="setFilterStatus(s)"><span>{{ STATUS[s] }}</span><strong>{{ nf(counts[s]) }}</strong></button>
    <div class="pool-sync"><span class="pool-live-dot"></span>{{ updatedAt ? '更新于 '+clock(updatedAt) : '正在读取' }}</div>
  </div>
  <n-alert v-if="error" type="error" :show-icon="false" style="margin-bottom:12px">{{ error }} <n-button text @click="reload">重试</n-button></n-alert>
  <div class="pool-table-surface">
    <div class="pool-filters">
      <n-input v-model:value="filters.q" clearable placeholder="搜索名称、账号、ID 或备注" aria-label="搜索账号"/>
      <n-select v-model:value="filters.channel" clearable placeholder="全部渠道" :options="channelOptions"/>
      <n-select v-model:value="filters.account_type" clearable placeholder="全部类型" :options="typeOptions"/>
      <n-select v-model:value="filters.status" clearable placeholder="全部状态" :options="Object.keys(STATUS).map(k=>({label:STATUS[k],value:k}))"/>
      <n-select v-model:value="filters.group_id" clearable placeholder="全部分组" :options="[{label:'未限定分组',value:'ungrouped'},...groupOptions]"/>
      <n-select v-model:value="filters.schedulable" clearable placeholder="全部调度状态" :options="[{label:'可调度',value:'true'},{label:'不可调度',value:'false'}]"/>
      <n-select v-model:value="filters.privacy_mode" clearable placeholder="全部隐私状态" :options="[{label:'已关闭模型训练',value:'training_off'},{label:'尚未设置',value:'__unset__'}]"/>
      <n-button quaternary @click="resetFilters">重置</n-button>
    </div>
    <SiteUsage scope="site" class="pool-live-metrics"/>
    <div class="pool-table-toolbar">
      <n-text depth="3">共 {{ nf(total) }} 个账号 <span v-if="selected.length">· 已选 {{ nf(selected.length) }} 个</span></n-text>
      <n-space :size="8" align="center">
        <n-select v-model:value="autoSecs" size="small" style="width:142px" :options="[{label:'不自动刷新',value:0},...[5,10,15,30,60].map(n=>({label:n+' 秒自动刷新',value:n}))]"/>
        <n-text v-if="autoSecs" depth="3" style="font-size:11px">{{ countdown }}s</n-text>
        <n-popover trigger="click" placement="bottom-end"><template #trigger><n-button size="small" secondary>显示列</n-button></template>
          <div class="pool-column-picker"><n-checkbox v-for="col in columnOptions" :key="col.key" :checked="!hidden.includes(col.key)" @update:checked="v=>changeColumn(col.key,v)">{{ col.title }}</n-checkbox></div>
        </n-popover>
      </n-space>
    </div>
    <div v-if="selected.length" class="pool-selection-bar">
      <div><strong>{{ allResults ? '已选择全部筛选结果' : '已选择' }} {{ nf(selected.length) }} 个账号</strong>
        <n-button v-if="!allResults && total>selected.length" text size="small" :loading="busy==='select'" @click="selectAllResults">选择全部 {{ nf(total) }} 个结果</n-button>
        <n-button text size="small" @click="clearSelection">清空</n-button></div>
      <n-space :size="6"><n-button size="small" @click="openBatch('resume')">启用</n-button><n-button size="small" @click="openBatch('pause')">停用</n-button>
        <n-button size="small" @click="openBatch('refresh')">刷新凭据</n-button><n-button size="small" @click="openBatch('recover')">恢复状态</n-button>
        <n-button size="small" @click="openBatch('test')">测试</n-button><n-button size="small" type="primary" secondary @click="openBatch('edit')">批量编辑</n-button>
        <n-button v-if="filters.pool_kind==='account'" size="small" @click="openBatch('sync')">同步 GPT 账号</n-button>
        <n-button size="small" type="error" secondary @click="openBatch('delete')">删除</n-button></n-space>
    </div>
    <n-data-table remote :columns="columns" :data="rows" :loading="loading" :bordered="false" size="small" :row-key="r=>r.id"
      :scroll-x="scrollWidth" :checked-row-keys="selected" @update:checked-row-keys="v=>{selected=v;allResults=false}" @update:sorter="updateSort"/>
    <div class="pool-table-footer"><n-text depth="3">账号倍率用于账号成本统计，用户扣费沿用用户分组规则。</n-text>
      <n-pagination v-model:page="page" v-model:page-size="pageSize" :item-count="total" :page-slot="5" show-size-picker :page-sizes="[20,50,100,200]"/>
    </div>
  </div>
  <p class="pool-stat-note">逐号用量从 {{ clock(summary.tracking_since) }} 开始记录。今日全站另有 {{ nf(summary.unassigned_today) }} 条未归属账号的日志，不计入任何账号。最后使用与巡检时间分别记录。</p>

  <n-modal v-model:show="editor.show" preset="card" :title="editor.id ? '编辑账号 · #'+editor.id : editor.sourceId ? '复制账号 · #'+editor.sourceId : '新建账号'" class="pool-editor-modal" :mask-closable="!editor.saving" :close-on-esc="!editor.saving" :closable="!editor.saving">
    <n-alert v-if="editor.error" type="error" :show-icon="false" style="margin-bottom:16px">{{ editor.error }}</n-alert>
    <n-form label-placement="top" :show-feedback="false" class="pool-form-grid">
      <n-form-item label="渠道"><n-select v-model:value="editor.form.channel" :options="channelOptions" :disabled="!!editor.id || !!editor.sourceId" placeholder="选择已有渠道"/></n-form-item>
      <n-form-item label="认证类型"><n-select v-model:value="editor.form.account_type" :options="editor.form.channel==='gpt' ? typeOptions.filter(o=>o.value==='oauth') : typeOptions"/></n-form-item>
      <n-form-item label="账号标识"><n-input v-model:value="editor.form.identity" :disabled="!!editor.id" placeholder="邮箱或唯一标识"/></n-form-item>
      <n-form-item label="显示名称"><n-input v-model:value="editor.form.name" placeholder="便于识别的账号名称" maxlength="100"/></n-form-item>
      <n-form-item label="优先级（越小越优先）"><n-input-number v-model:value="editor.form.priority" :min="0" :precision="0"/></n-form-item>
      <n-form-item label="最大并发（0 为不限）"><n-input-number v-model:value="editor.form.concurrency" :min="0" :precision="0"/></n-form-item>
      <n-form-item label="账号计费倍率"><n-input-number v-model:value="editor.form.rate_multiplier" :min="0" :step="0.1"/></n-form-item>
      <n-form-item label="允许的用户分组"><n-select v-model:value="editor.form.group_ids" multiple clearable :options="groupOptions" placeholder="不限分组"/></n-form-item>
      <n-form-item label="账号到期时间"><n-date-picker v-model:value="expiryMillis" type="datetime" clearable/></n-form-item>
      <n-form-item label="到期策略"><n-checkbox v-model:checked="editor.form.auto_pause_on_expired">到期自动暂停调度</n-checkbox></n-form-item>
      <n-form-item label="总成本额度（0 为不限）"><n-input-number v-model:value="editor.form.quota_limit" :min="0"/></n-form-item>
      <n-form-item label="每日成本额度"><n-input-number v-model:value="editor.form.quota_daily_limit" :min="0"/></n-form-item>
      <n-form-item label="近 7 天成本额度"><n-input-number v-model:value="editor.form.quota_weekly_limit" :min="0"/></n-form-item>
      <n-form-item label="近 5 小时成本额度"><n-input-number v-model:value="editor.form.window_cost_limit" :min="0"/></n-form-item>
      <n-form-item label="每分钟请求上限（0 为不限）"><n-input-number v-model:value="editor.form.base_rpm" :min="0" :precision="0"/></n-form-item>
      <n-form-item label="代理"><n-select v-model:value="editor.form.proxy_id" clearable :disabled="!selectedChannel || !selectedChannel.can_proxy" :options="proxyOptions" placeholder="直连"/></n-form-item>
      <n-form-item label="调度开关"><n-checkbox v-model:checked="editor.form.paused">管理员停用调度</n-checkbox></n-form-item>
      <n-form-item label="备注" class="pool-span-2"><n-input v-model:value="editor.form.note" type="textarea" :autosize="{minRows:2,maxRows:4}" maxlength="2000"/></n-form-item>
      <n-form-item v-if="!editor.form.parent_account_id" :label="editor.id ? '更新凭据（留空保留现有凭据）' : editor.sourceId ? '访问凭据（留空复制来源账号的静态凭据）' : '访问凭据'" class="pool-span-2"><n-input v-model:value="editor.form.credentials" type="textarea" :autosize="{minRows:4,maxRows:9}" placeholder='粘贴 API Key、access_token，或完整 JSON 凭据对象；支持 Agent Identity' :input-props="{spellcheck:false,autocomplete:'off'}"/></n-form-item>
      <GPTSettings v-if="editor.form.channel==='gpt' && editor.id" :key="editKey" v-model:value="editor.form.gpt_settings" :shadow="editor.form.parent_account_id" class="pool-span-2"/>
    </n-form>
    <template #footer><n-space justify="end"><n-button :disabled="editor.saving" @click="editor.show=false">取消</n-button><n-button type="primary" :loading="editor.saving" @click="saveEditor">保存账号</n-button></n-space></template>
  </n-modal>

  <n-modal v-model:show="batch.show" preset="card" :title="ACTION[batch.action]+' · 操作预览'" class="pool-editor-modal" :mask-closable="!batch.running" :close-on-esc="!batch.running" :closable="!batch.running">
    <n-alert :type="batch.action==='delete' ? 'error' : 'info'" :show-icon="false">将对预览中的 {{ nf(batch.targets.length) }} 个账号执行「{{ ACTION[batch.action] }}」。<span v-if="batch.action==='delete'">账号将永久移除，历史用量保留。</span><span v-if="batch.action==='test'">每个账号会真实调用一次上游，可能消耗上游额度。</span></n-alert>
    <n-alert v-if="batch.error" type="error" :show-icon="false" style="margin-top:12px">{{ batch.error }}</n-alert>
    <div v-if="batch.edit && !batch.results.length" class="pool-batch-fields">
      <p>只修改勾选的字段，其他设置保持原值。</p>
      <div v-for="key in ['priority','concurrency','rate_multiplier','quota_daily_limit']" :key="key"><n-checkbox v-model:checked="batchFields[key]">{{ {priority:'优先级',concurrency:'最大并发',rate_multiplier:'账号倍率',quota_daily_limit:'每日成本额度'}[key] }}</n-checkbox><n-input-number v-model:value="batchForm[key]" :disabled="!batchFields[key]" :min="0"/></div>
      <div><n-checkbox v-model:checked="batchFields.group_ids">用户分组</n-checkbox><n-select v-model:value="batchForm.group_ids" multiple :disabled="!batchFields.group_ids" :options="groupOptions" placeholder="不限分组"/></div>
      <div><n-checkbox v-model:checked="batchFields.proxy_id">代理</n-checkbox><n-select v-model:value="batchForm.proxy_id" clearable :disabled="!batchFields.proxy_id" :options="proxyOptions" placeholder="直连"/></div>
      <div><n-checkbox v-model:checked="batchFields.note">备注</n-checkbox><n-input v-model:value="batchForm.note" :disabled="!batchFields.note"/></div>
    </div>
    <n-spin :show="busy==='preview'"><div class="pool-target-list"><div v-for="r in batch.targets" :key="r.id"><span>#{{ r.id }}</span><strong>{{ r.name }}</strong><span>{{ r.channel }}</span></div></div></n-spin>
    <n-progress v-if="batch.running || batch.results.length" type="line" :percentage="batch.targets.length ? Math.round(batch.done/batch.targets.length*100) : 0"/>
    <p v-if="batch.results.length">已完成 {{ nf(batch.done) }} / {{ nf(batch.targets.length) }} · 成功 {{ nf(batch.results.length-failedResults.length) }} · 失败 {{ nf(failedResults.length) }}</p>
    <div v-if="failedResults.length" class="pool-operation-results"><div v-for="r in failedResults" :key="r.id">#{{ r.id }} · {{ r.message }}</div></div>
    <template #footer><n-space justify="end"><n-button :disabled="batch.running" @click="batch.show=false">{{ batch.results.length ? '关闭' : '取消' }}</n-button><n-button v-if="!batch.results.length" :type="batch.action==='delete' ? 'error' : 'primary'" :loading="batch.running" :disabled="!batch.targets.length || busy==='preview'" @click="runBatch">确认{{ ACTION[batch.action] }}</n-button></n-space></template>
  </n-modal>

  <n-modal v-model:show="test.show" preset="card" title="账号测试" class="pool-editor-modal" :mask-closable="!test.running" :close-on-esc="false" @esc="closeTestOnEsc" :closable="!test.running">
    <div v-if="test.row" class="pool-test-identity"><Ic name="user" :size="18"/><strong>{{ test.row.name }}</strong><span>#{{ test.row.id }}</span></div>
    <div v-if="test.row && test.row.channel==='gpt'" class="pool-test-modes" role="group" aria-label="测试方式">
      <button type="button" :aria-pressed="test.kind==='chat'" :disabled="test.running" @click="setTestKind('chat')"><Ic name="response" :size="16"/>对话测试</button>
      <button type="button" :aria-pressed="test.kind==='image'" :disabled="test.running" @click="setTestKind('image')"><Ic name="image" :size="16"/>生图测试</button>
    </div>
    <div class="pool-model-picker"><n-select v-model:value="test.model" filterable :tag="test.row && test.row.channel==='gpt'" :disabled="test.running" :loading="test.row && modelLoading[test.row.id]" :options="modelsFor(test.row,test.kind)" placeholder="选择上游模型"/><n-button v-if="test.kind==='chat' && test.row && test.row.channel==='gpt'" secondary :disabled="test.running || modelScan(test.row).running" :loading="modelScan(test.row).running" @click="loadTestModels(test.row,true)" aria-label="检测可用模型">检测可用模型</n-button></div>
    <p class="pool-field-hint">{{ test.kind==='image' ? 'Web 生图使用此账号的 ChatGPT 登录凭据；Codex 生图使用所选对话模型的图片工具。' : modelHint(test.row) }}</p>
    <n-progress v-if="modelScan(test.row).running" type="line" :percentage="modelScan(test.row).total ? Math.round(100*modelScan(test.row).done/modelScan(test.row).total) : 0" :show-indicator="false" :height="3"/>
    <div v-if="test.kind==='image'" class="pool-image-prompt"><label for="pool-image-prompt">生图描述</label><n-input id="pool-image-prompt" v-model:value="test.prompt" type="textarea" :disabled="test.running" :autosize="{minRows:2,maxRows:5}" maxlength="2000" placeholder="描述想生成的图片"/></div>
    <div class="pool-test-conversation" aria-live="polite">
      <div v-if="test.kind==='chat'" class="pool-chat-question"><span>发送内容</span><p>hi</p></div>
      <div v-if="test.running" class="pool-chat-answer" role="status"><span>测试中 · {{ test.elapsed }} 秒</span><p class="pool-waiting">{{ test.kind==='image' ? test.progress+'…' : '正在等待上游回答…' }}</p><small v-if="test.kind==='image'">有生成进度会继续等待；长时间无输出才会超时。</small><small v-else-if="test.row.channel==='gpt'">包含账号排队，最多等待 30 秒；账号正忙会提前提示。</small></div>
      <div v-else-if="test.result && test.result.ok" class="pool-chat-answer"><span>{{ test.result.kind==='image' ? '生成结果' : '模型回答' }} <small>{{ test.result.model }} · {{ (test.result.elapsed_ms/1000).toFixed(2) }} s</small></span>
        <div v-if="test.result.kind==='image'" class="pool-image-preview"><n-image v-for="(image,index) in test.result.images" :key="index" :src="'data:'+image.mime_type+';base64,'+image.b64_json" :alt="'生图测试结果 '+(index+1)" object-fit="contain"/><small>点击图片放大查看</small></div>
        <pre v-else>{{ test.result.reply }}</pre>
      </div>
    </div>
    <p class="pool-field-hint">{{ test.kind==='image' ? '生成一张测试图片，会使用上游额度；本站不扣用户余额。' : '发送一次 hi，会使用上游额度；回答保存在测试记录中，本站不扣用户余额。' }}</p>
    <n-alert v-if="test.error" type="error" :show-icon="false">{{ test.error }}</n-alert>
    <n-alert v-if="test.result && !test.result.ok" :type="test.result.error_kind==='capacity' ? 'warning' : 'error'" :show-icon="false">{{ test.result.error }}</n-alert>
    <template #footer><n-button type="primary" :loading="test.running" :disabled="!test.model || (test.kind==='image' && !test.prompt.trim())" @click="runTest">{{ test.kind==='image' ? '生成测试图片' : '发送测试请求' }}</n-button></template>
  </n-modal>

  <GPTOperations v-if="resetRow" :key="resetRow.id" :row="resetRow" :reset-only="true" @close="resetRow=null" @saved="reload()"/>

  <n-drawer v-model:show="detail.show" :width="'min(920px,100vw)'" class="pool-detail-drawer"><n-drawer-content title="账号详情" closable>
    <n-alert v-if="detail.error" type="error" :show-icon="false">{{ detail.error }}</n-alert>
    <div v-if="detail.row" class="pool-detail-identity">
      <div class="pool-identity-mark"><Ic name="user" :size="23"/></div>
      <div class="pool-identity-copy"><h2>{{ detail.row.name }}</h2><div><span>#{{ detail.row.id }}</span><span>{{ detail.row.channel==='gpt' ? 'OpenAI' : detail.row.channel }} · {{ TYPE[detail.row.account_type] }}</span><n-tag size="small" :type="detail.row.schedulable ? 'success' : 'warning'" :bordered="false">{{ detail.row.status_label }}</n-tag></div></div>
      <n-button secondary size="small" @click="openEditor(detail.row)"><template #icon><Ic name="pencil" :size="14"/></template>编辑</n-button>
    </div>
    <n-spin :show="detail.loading"><n-tabs v-model:value="detail.tab" type="line" animated>
      <n-tab-pane v-if="detail.row?.channel==='gpt'" name="gpt" tab="上游操作"><GPTOperations v-if="detail.show && detail.tab==='gpt'" :key="detail.row.id" :row="detail.row" @saved="reload();openDetails(detail.row,'gpt')"/></n-tab-pane>
      <n-tab-pane name="info" tab="基本信息"><template v-if="detail.row">
        <section v-if="detail.row.channel==='gpt'" class="pool-upstream-panel" aria-label="GPT 上游资料与额度">
          <div class="pool-section-heading"><h3><Ic name="gauge" :size="17"/>上游套餐与额度</h3><n-button text size="small" :loading="!!syncing[detail.row.id]" @click="syncAccount(detail.row,true)">同步上游</n-button></div>
          <div class="pool-upstream-summary">
            <div class="pool-upstream-plan"><strong>{{ planLabel(detail.row.plan_type) }}</strong><span>{{ detail.row.workspace_name || 'GPT 账号' }}</span><small>订阅至 {{ clock(detail.row.subscription_expires_at) }}</small></div>
            <div><Ic name="coins" :size="18"/><strong>{{ creditsValue(detail.row) }}</strong><span>Codex 积分</span></div>
            <div><Ic name="gift" :size="18"/><strong>{{ detail.row.gpt_reset_credits ? detail.row.gpt_reset_credits.available_count : '—' }}</strong><span>可用重置券</span></div>
          </div>
          <div v-for="window in quotaWindows(detail.row)" :key="window.label" class="pool-detail-window">
            <b>{{ window.label }}</b><span class="pool-quota-track"><i :class="window.tone" :style="{width:window.width+'%'}"></i></span><strong>{{ window.value==null ? '—' : window.value+'%' }}</strong><small :title="clock(window.reset)">{{ window.reset ? clock(window.reset)+' 重置' : '重置时间未提供' }}</small>
          </div>
          <p v-if="!quotaWindows(detail.row).length" class="pool-field-hint">上游尚未返回用量窗口</p>
          <p v-if="detail.row.gpt_reset_credits && detail.row.gpt_reset_credits.expirations.length" class="pool-field-hint">重置券到期：{{ detail.row.gpt_reset_credits.expirations.map(clock).join('、') }}</p>
        </section>
        <div class="pool-info-sections"><section v-for="section in detailSections(detail.row)" :key="section.title" class="pool-info-section">
          <h3><Ic :name="section.icon" :size="17"/>{{ section.title }}</h3><dl><div v-for="field in section.fields" :key="field[0]" :class="{'pool-fact-wide':field[2]}"><dt>{{ field[0] }}</dt><dd>{{ field[1] }}</dd></div></dl>
        </section></div>
        <details class="pool-credential-details"><summary><Ic name="key" :size="15"/>认证字段</summary><p>{{ detail.row.credential_fields.join(' · ') || '无' }}</p></details>
        <n-alert v-if="detail.row.last_error" type="warning" :show-icon="false" style="margin-top:16px">{{ detail.row.last_error }}</n-alert>
      </template></n-tab-pane>
      <n-tab-pane name="stats" tab="用量统计"><template v-if="detail.stats">
        <div class="pool-section-heading pool-stats-heading"><h3>本站调用概览</h3><span>近 30 天 · 此账号</span></div>
        <div class="pool-detail-metrics">
          <div><span><Ic name="response" :size="15"/>请求</span><strong>{{ nf(detail.stats.summary.requests) }}</strong></div>
          <div><span><Ic name="database" :size="15"/>Token</span><strong :title="nf(detail.stats.summary.tokens)">{{ compact(detail.stats.summary.tokens) }}</strong><small>含已记录缓存读写</small></div>
          <div><span><Ic name="wallet" :size="15"/>账号计费</span><strong :title="'官方原价 ' + usd(detail.stats.summary.standard_cost)">{{ usd(detail.stats.summary.cost) }}</strong><small>原价 {{ usd(detail.stats.summary.standard_cost) }} × 账号倍率</small></div>
          <div><span><Ic name="user" :size="15"/>用户计费</span><strong :title="'官方原价 ' + usd(detail.stats.summary.standard_cost)">{{ usd(detail.stats.summary.user_cost) }}</strong><small>原价 {{ usd(detail.stats.summary.standard_cost) }} × 分组倍率</small></div>
        </div>
        <div v-if="!hasStats" class="pool-detail-empty"><span class="pool-empty-mark"><Ic name="bars" :size="28"/></span><h3>还没有本站调用记录</h3><p>通过网关调用后，趋势、模型分布与请求明细会显示在这里。<br>账号测试单独保存在“测试与计划”，不计入调用统计。</p><n-button secondary @click="openTest(detail.row)">发送 hi 测试账号</n-button></div>
        <template v-else>
          <section class="pool-detail-section"><div class="pool-section-heading"><h3>每日请求</h3><span>近 30 天</span></div><BarChart :items="chartItems" color="var(--console-accent)" :height="130"/></section>
          <div class="pool-window-totals"><div v-for="key in ['5h','7d']" :key="key"><span><Ic name="clock" :size="14"/>近 {{ key }}</span><b>{{ nf(detail.stats.windows[key].requests) }} 次</b><small>{{ compact(detail.stats.windows[key].tokens) }} Token · {{ usd(detail.stats.windows[key].cost) }}</small></div></div>
          <section v-if="detail.stats.models.length" class="pool-detail-section"><div class="pool-section-heading"><h3>模型分布</h3><span>{{ detail.stats.models.length }} 个模型</span></div><n-table size="small" :bordered="false"><thead><tr><th>模型</th><th>请求</th><th>Token</th><th>账号计费</th></tr></thead><tbody><tr v-for="r in detail.stats.models" :key="r.model"><td>{{ r.model }}</td><td>{{ nf(r.requests) }}</td><td>{{ compact(r.tokens) }}</td><td>{{ usd(r.cost) }}</td></tr></tbody></n-table></section>
          <section v-if="detail.stats.recent.length" class="pool-detail-section"><div class="pool-section-heading"><h3>最近请求</h3><span>最近 {{ detail.stats.recent.length }} 条</span></div><n-table size="small" :bordered="false"><thead><tr><th>时间</th><th>模型</th><th>输入 / 输出</th><th>耗时</th></tr></thead><tbody><tr v-for="r in detail.stats.recent" :key="r.id"><td>{{ clock(r.created_at) }}</td><td>{{ r.model }}</td><td>{{ nf(r.input_tokens) }} / {{ nf(r.output_tokens) }}</td><td>{{ (r.duration_ms/1000).toFixed(2) }} s</td></tr></tbody></n-table></section>
          <p class="pool-field-hint">仅统计已归属此账号的本站请求；上游用量与订阅额度见“基本信息”。</p>
        </template>
      </template></n-tab-pane>
      <n-tab-pane name="tests" tab="测试与计划"><template v-if="detail.row">
        <section class="pool-plan-panel"><div class="pool-section-heading"><h3><Ic name="clock" :size="17"/>测试计划</h3><span>固定发送 hi，保存原文回答</span></div>
        <n-form label-placement="top" :show-feedback="false" class="pool-plan-form">
          <n-form-item label="测试模型"><div class="pool-model-picker"><n-select v-model:value="planForm.model" filterable :tag="detail.row.channel==='gpt'" :loading="!!modelLoading[detail.row.id]" :options="modelsFor(detail.row)" placeholder="选择上游模型"/><n-button v-if="detail.row.channel==='gpt'" secondary :disabled="modelScan(detail.row).running" :loading="modelScan(detail.row).running" @click="loadTestModels(detail.row,true)" aria-label="检测可用模型">检测可用模型</n-button></div></n-form-item>
          <n-form-item label="间隔"><n-select v-model:value="planForm.interval_seconds" :options="[{label:'5 分钟',value:300},{label:'30 分钟',value:1800},{label:'1 小时',value:3600},{label:'6 小时',value:21600},{label:'24 小时',value:86400}]"/></n-form-item>
        </n-form>
        <p class="pool-field-hint">{{ modelHint(detail.row) }}</p>
    <n-progress v-if="modelScan(detail.row).running" type="line" :percentage="modelScan(detail.row).total ? Math.round(100*modelScan(detail.row).done/modelScan(detail.row).total) : 0" :show-indicator="false" :height="3"/>
        <div class="pool-plan-actions"><label><n-switch v-model:value="planForm.enabled" size="small" aria-label="启用定时测试"/><span>定时测试</span></label><n-space><n-button secondary :disabled="!planForm.model || !canTest(detail.row)" @click="openTest(detail.row,planForm.model)">测试一次</n-button><n-button type="primary" :loading="busy==='plan'" :disabled="!planForm.model || !canTest(detail.row)" @click="savePlan">保存计划</n-button></n-space></div>
        <p class="pool-field-hint">{{ detail.plan && detail.plan.enabled ? '下次测试：'+clock(detail.plan.next_run)+' · ' : '' }}测试使用上游额度，本站不扣用户余额。</p></section>
        <section class="pool-detail-section"><div class="pool-section-heading"><h3>最近测试</h3><span>{{ detail.history.length }} 条记录</span></div>
          <div v-if="!detail.history.length" class="pool-history-empty"><Ic name="response" :size="21"/><span>测试后，这里会保留模型的实际回答</span></div>
          <details v-for="(r,index) in detail.history" :key="r.id" class="pool-test-record" :open="index===0"><summary><Ic :name="r.ok ? 'circleCheck' : 'alert'" :size="17" :class="r.ok ? 'is-success' : 'is-error'"/><strong>{{ r.kind==='image' ? '生图 · ' : '' }}{{ r.model }}</strong><time>{{ clock(r.created_at) }}</time><span>{{ (r.elapsed_ms/1000).toFixed(2) }} s</span><Ic name="chevronDown" :size="14"/></summary><div class="pool-test-record-body"><small>{{ r.prompt ? '发送：'+r.prompt : '旧版测试请求' }}</small><pre>{{ r.ok ? r.reply || '此条旧记录没有保存文本回答' : r.error }}</pre></div></details>
        </section>
      </template></n-tab-pane>
    </n-tabs></n-spin>
  </n-drawer-content></n-drawer>

  <GPTPAT v-model:show="patShow" :proxies="proxyOptions" @saved="reload()"/>
  <n-modal v-model:show="transfer.show" preset="card" title="导入账号" class="pool-editor-modal" :mask-closable="!transfer.running" :close-on-esc="!transfer.running" :closable="!transfer.running">
    <p class="pool-stat-note">接受 bit-api / sub2api 账号导出、Codex auth.json、JSON 数组或逐行 Key。GPT 另支持「邮箱----密码----二步验证密钥----access_token」，仅保存邮箱与 Token。原始凭据仅发送到本站。</p>
    <n-space vertical :size="14"><n-select v-model:value="transfer.channel" :disabled="transfer.running" clearable :options="channelOptions" placeholder="自动识别渠道；裸 Key 需选择目标渠道"/>
      <input type="file" accept=".json,.jsonl,.txt" multiple :disabled="transfer.running" @change="readFiles" aria-label="选择账号导入文件"/>
      <n-space v-if="transfer.filenames.length"><n-tag v-for="(name,index) in transfer.filenames" :key="index" :closable="!transfer.running" @close="()=>{transfer.filenames.splice(index,1);transfer.contents=transfer.contents.filter((_,i)=>i!==index)}">{{ name }}</n-tag></n-space>
      <n-input v-model:value="transfer.content" type="textarea" :disabled="transfer.running" :autosize="{minRows:8,maxRows:14}" placeholder="粘贴导出内容或凭据文本" :input-props="{spellcheck:false,autocomplete:'off'}"/>
      <n-checkbox v-model:checked="transfer.update_existing" :disabled="transfer.running">更新已有账号的凭据与设置（默认跳过）</n-checkbox>
      <n-alert v-if="transfer.error" type="error" :show-icon="false">{{ transfer.error }}</n-alert>
      <template v-if="transfer.result"><n-alert :type="transfer.result.failed ? 'warning' : 'success'" :show-icon="false">{{ transfer.result.applied ? '导入结果' : '预览结果' }}：新增 {{ transfer.result.imported }} · 更新 {{ transfer.result.updated }} · 跳过 {{ transfer.result.skipped }} · 失败 {{ transfer.result.failed }}</n-alert>
        <div class="pool-operation-results"><div v-for="r in transfer.result.items" :key="r.index">#{{ r.index }} {{ r.name || '' }} · {{ {imported:'新增',updated:'更新',skipped:'跳过',failed:'失败'}[r.action] }} {{ r.message }}</div></div>
      </template>
    </n-space>
    <template #footer><n-space justify="end"><n-button :loading="transfer.running" :disabled="!transfer.content.trim() && !transfer.contents.length" @click="previewImport(false)">预览导入</n-button><n-button type="primary" :loading="transfer.running" :disabled="!transfer.result || transfer.result.applied || !transfer.result.imported && !transfer.result.updated" @click="previewImport(true)">确认导入</n-button></n-space></template>
  </n-modal>
  <n-modal v-model:show="proxy.show" preset="card" title="账号代理" class="pool-editor-modal">
    <p class="pool-stat-note">名称可选填，地址自动识别格式；未带协议时使用 HTTP。</p>
    <n-alert v-if="proxy.error" type="error" :show-icon="false">{{ proxy.error }}</n-alert>
    <div class="pool-form-grid">
      <n-input v-model:value="proxy.name" placeholder="代理名称（选填）" maxlength="100" :input-props="{'aria-label':'代理名称'}"/>
      <n-input v-model:value="proxy.url" type="password" show-password-on="click" placeholder="粘贴代理地址（自动识别格式）" :input-props="{'aria-label':'代理地址'}" @keydown.enter="proxy.url.trim() && !proxy.running && saveProxy()"/>
    </div>
    <n-button type="primary" :loading="proxy.running" :disabled="!proxy.url.trim()" @click="saveProxy" style="margin:14px 0">保存代理</n-button>
    <p class="pool-stat-note">测试代理到 GPT 接口的网络连通性，延迟为收到接口响应的耗时。</p>
    <n-table size="small" :bordered="false"><thead><tr><th>名称</th><th>地址</th><th>账号数</th><th>GPT 连通性</th><th>操作</th></tr></thead>
      <tbody><tr v-for="p in options.proxies" :key="p.id"><td>{{ p.name }}</td><td>{{ p.address }}</td><td>{{ p.accounts }}</td>
        <td><template v-if="proxyTests[p.id] && !proxyTests[p.id].running">
          <n-tooltip v-if="proxyTests[p.id].ok"><template #trigger><n-tag type="success" size="small" :bordered="false">已连通 · {{ nf(proxyTests[p.id].latency_ms) }} ms</n-tag></template>{{ proxyTests[p.id].message }}</n-tooltip>
          <template v-else><n-tag type="error" size="small" :bordered="false">连接失败</n-tag><p class="pool-stat-note" style="max-width:220px;margin:6px 0 0">{{ proxyTests[p.id].message }}</p></template>
        </template><n-text v-else depth="3">{{ proxyTests[p.id] ? '连接中…' : '未测试' }}</n-text></td>
        <td><n-space :size="12"><n-button text type="primary" :loading="!!(proxyTests[p.id] && proxyTests[p.id].running)" @click="testProxy(p.id)">测试 GPT</n-button><n-popconfirm @positive-click="removeProxy(p.id)"><template #trigger><n-button text type="error" :disabled="p.accounts>0">删除</n-button></template>删除此代理配置？</n-popconfirm></n-space></td>
      </tr></tbody>
    </n-table>
  </n-modal>
  <n-modal v-model:show="pausing.show" preset="card" title="临时暂停调度" class="pool-editor-modal"><n-space vertical :size="16"><n-input-number v-model:value="pausing.minutes" :min="1" :max="43200"><template #suffix>分钟</template></n-input-number><n-input v-model:value="pausing.reason" placeholder="暂停原因" maxlength="500"/><n-button type="primary" :loading="busy==='temporary'" @click="saveTemporary">确认暂停</n-button></n-space></n-modal>
  <n-modal v-model:show="oauth.show" preset="card" :title="oauth.row ? '重新授权 · '+oauth.row.name : '添加 GPT OAuth 账号'" class="pool-editor-modal" :mask-closable="!oauth.running" :close-on-esc="!oauth.running" :closable="!oauth.running">
    <n-space vertical :size="16">
      <n-alert v-if="oauth.error" type="error" :show-icon="false">{{ oauth.error }}</n-alert>
      <n-select v-model:value="oauth.proxy_id" clearable :disabled="!!oauth.session_id" :options="proxyOptions" placeholder="凭据交换使用的代理（默认直连）"/>
      <n-button :loading="oauth.running" @click="startOAuth">{{ oauth.session_id ? '重新生成授权链接' : '生成授权链接' }}</n-button>
      <template v-if="oauth.auth_url"><n-alert type="info" :show-icon="false">打开授权页并完成登录。浏览器跳到 localhost:1455 后，即使显示无法连接，也请复制地址栏里的完整地址粘贴到下面。授权会话 30 分钟内有效。</n-alert>
        <a :href="oauth.auth_url" target="_blank" rel="noreferrer noopener" class="pool-oauth-link">打开 OpenAI 授权页 ↗</a>
        <n-input v-model:value="oauth.callback_url" type="textarea" :autosize="{minRows:3,maxRows:6}" placeholder="http://localhost:1455/auth/callback?code=...&state=..."/>
        <n-button type="primary" :loading="oauth.running" :disabled="!oauth.callback_url.trim()" @click="finishOAuth">交换凭据并保存账号</n-button>
      </template>
    </n-space>
  </n-modal>
</div>`,
  };
  window.BitPortalPool = { Pool };
  window.BitPortalAdmin.Admin.components.Pool = Pool;
})();
