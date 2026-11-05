/* 控制台用户侧页面:概览 / 密钥 / 使用记录 / 钱包 / 计费 / 邀请 / 个人资料。
   数据全部来自 /api/*。 */
(function () {
  "use strict";
  const { h, ref, reactive, computed, onMounted, onUnmounted, watch } = Vue;
  const P = window.BitPortal;
  const { api, msg, dlg, copy, nf, usd, kf, fmt, MONO, SUBSTY, absTime, dayTime,
    relTime, st, POLICY, MODE, tag, money, reasonOf, ic, Ic, C, CT, tipRow, wrapTip,
    subline, stack, box, modelChip, Pills, StatCard,
    prov, provIcon, Avatar, shrinkAvatar,
    tpsOf, normUsage, usageColumns, END_TEXT, EXPIRY_PRESETS,
    planDur, planKind, planLeft,
    CCSWITCH_APPS, ccServerAddress, buildCCSwitchURL, ccLoadModels, go,
    BASE, token, setToken, qs, download } = P;

  const dash = (v) => (v == null ? "—" : v);
  const pxCell = (v) => (v == null ? "—" : "$" + Number(v).toFixed(4));
  const API_ENDPOINT = "https://ai.baipiao.co/";
  const MONO_ATTR = { style: MONO };

  /* 页面级加载/报错壳:一处写好,7 个页面不各写一遍 spin 和空态。 */
  const Load = {
    props: ["loading", "error", "onRetry"],
    setup(props) {
      const loaded = ref(!props.loading);
      watch(() => props.loading, (busy) => { if (!busy) loaded.value = true; });
      return { loaded };
    },
    render() {
      if (this.error)
        return h(naive.NResult, { status: "error", title: "加载失败",
          description: String(this.error) }, { footer: () =>
            h(naive.NButton, { onClick: this.onRetry }, () => "重试") });
      if (this.loading && !this.loaded)
        return h("div", { class: "console-loading", role: "status", "aria-label": "正在加载数据", "aria-busy": "true" }, [
          h("span", { class: "console-sr-only" }, "正在加载数据"),
          h("div", { class: "console-loading-head", "aria-hidden": "true" }),
          h("div", { class: "console-loading-value", "aria-hidden": "true" }),
          h("div", { class: "console-loading-grid", "aria-hidden": "true" }, [1, 2, 3].map(() => h("div", { class: "console-loading-block" }))),
        ]);
      return h(naive.NSpin, { show: !!this.loading, style: "width:100%", "aria-busy": !!this.loading },
        () => h("div", { style: this.loading ? "min-height:220px" : null },
          this.$slots.default ? this.$slots.default() : null));
    },
  };

  /* 统一取数:load() 只写「怎么拿」,loading/error/首屏 onMounted 都在这里。 */
  function useFetch(loader) {
    const loading = ref(true);
    const error = ref("");
    const run = () => {
      loading.value = true;
      error.value = "";
      return Promise.resolve().then(loader).catch((e) => {
        error.value = e.message || String(e);
      }).then(() => { loading.value = false; });
    };
    onMounted(run);
    return { loading, error, reload: run };
  }
  const Dashboard = {
    components: { Load, StatCard, Pills, Ic, ModelCachePanel: P.ModelCachePanel },
    setup() {
      const o = ref({});
      const sum = ref({});
      const s = useFetch(() => Promise.all([api("GET", "/overview"),
        api("GET", "/usage?range=day")]).then((r) => {
          o.value = r[0];
          sum.value = r[1].summary || {};
        }));
      const policy = computed(() => o.value.policy || "free");
      const today = computed(() => o.value.today || {});
      const total = computed(() => o.value.total || {});
      const perf = computed(() => o.value.perf || {});
      const keys = computed(() => o.value.keys || { total: 0, active: 0 });
      const checkin = computed(() => o.value.checkin || { range: {} });
      const checkingIn = ref(false);
      const doCheckin = () => {
        if (checkingIn.value || checkin.value.checked_in) return;
        checkingIn.value = true;
        api("POST", "/checkin").then((r) => {
          msg.success(r.claimed
            ? "签到成功，获得 $" + fmt(r.reward, 4)
            : "今天已经签到过了，已领取 $" + fmt(r.reward, 4));
          window.dispatchEvent(new CustomEvent("bitapi:balance-changed"));
          return s.reload();
        }).catch((e) => msg.error(e.message))
          .then(() => { checkingIn.value = false; });
      };
      const tokOf = (x) => (x.input_tokens || 0) + (x.output_tokens || 0) +
        (x.cache_tokens || 0);
      /* 缓存 token 只能由 snapshot 的 total_ctx 反推,没有 snapshot 的行算不出。
         算不全就如实说明,不把「不知道」显示成 0。 */
      const cacheNote = (x) => {
        const n = x.cache_rows || 0;
        const all = x.requests || 0;
        if (!all) return "缓存 0";
        return "缓存 " + kf(x.cache_tokens || 0) +
          (n < all ? "(" + n + "/" + all + " 条可算)" : "");
      };

      /* 账户摘要置顶，调用与性能指标单独成组。 */
      const cards = computed(() => {
        const list = [];
        const cacheTokens = total.value.cache_tokens;
        const totalInput = cacheTokens == null || total.value.input_tokens == null ? null
          : Number(total.value.input_tokens) + Number(cacheTokens);
        const cacheKnown = (total.value.cache_rows || 0) > 0 && totalInput > 0;
        if (policy.value === "balance")
          list.push({ label: "余额", value: "$" + fmt(o.value.balance),
            sub: "累计消费 $" + fmt(o.value.total_spent),
            color: "#15803d", icon: "wallet", tint: true });
        else
          list.push({ label: "计费策略", value: POLICY[policy.value] || policy.value,
            sub: o.value.group ? "分组 " + o.value.group : "未分配分组",
            color: "#3f6b8a", icon: "tag" });
        list.push({ label: "API 密钥", value: nf(keys.value.total), animate: true,
          sub: keys.value.active + " 启用 · " +
            (keys.value.total - keys.value.active) + " 失效",
          color: "#4a6fa5", icon: "key" });
        list.push({ label: "今日请求", value: nf(today.value.requests || 0), animate: true,
          sub: "累计 " + nf(total.value.requests || 0),
          color: "#3f6b8a", icon: "bars" });
        /* 原价与实扣并排:倍率不为 1 时两者不同,只显示一个会让人对不上账。 */
        list.push({ label: "今日消费",
          value: "$" + fmt(today.value.actual_cost || 0),
          value2: "$" + fmt(today.value.cost || 0),
          sub: "累计 $" + fmt(total.value.actual_cost || 0) +
            " / $" + fmt(total.value.cost || 0),
          color: "#6b5b95", icon: "dollar", tone: "accent" });
        list.push({ label: "今日 Token", value: kf(tokOf(today.value)), animate: true,
          sub: "输入 " + kf(today.value.input_tokens || 0) + " · 输出 " +
            kf(today.value.output_tokens || 0) + " · " + cacheNote(today.value),
          color: "#a17a10", icon: "cube" });
        list.push({ label: "累计 Token", value: kf(tokOf(total.value)), animate: true,
          sub: "输入 " + kf(total.value.input_tokens || 0) + " · 输出 " +
            kf(total.value.output_tokens || 0) + " · " + cacheNote(total.value),
          color: "#6b5b95", icon: "coins" });
        list.push({ label: "缓存率",
          value: cacheKnown ? fmt(Number(cacheTokens) / totalInput * 100, 1) + "%" : "—",
          sub: "个人累计 · 缓存 ÷ 总输入，不含输出",
          sub2: cacheKnown ? "缓存 " + kf(cacheTokens) + " / 总输入 " + kf(totalInput) +
            ((total.value.cache_rows || 0) < (total.value.requests || 0)
              ? " · " + total.value.cache_rows + "/" + total.value.requests + " 条缓存可算" : "")
            : "暂无可计算的缓存记录",
          color: "#4a6fa5", icon: "database" });
        /* 速率小于 10 时给两位小数:演示量级下 round 成整数会一律显示 0 或 1,
           看不出差别;上到几百以后小数没意义,改千分位。 */
        const rate = (v) => (v < 10 ? fmt(v, 2) : nf(Math.round(v)));
        list.push({ label: "性能指标", color: "#6b5b95", icon: "bolt",
          rows: [["RPM", rate(perf.value.rpm || 0), undefined],
            ["TPM", rate(perf.value.tpm || 0), "#6b5b95"]],
          sub: perf.value.span_minutes
            ? "按 " + fmt(perf.value.span_minutes / 60, 1) + " 小时跨度平摊"
            : "暂无调用记录" });
        list.push({ label: "平均响应",
          value: perf.value.samples
            ? fmt((perf.value.avg_ms || 0) / 1000, 2) + "s" : "—",
          sub: perf.value.samples ? perf.value.samples + " 次有耗时记录"
            : "暂无耗时样本",
          tone: perf.value.samples ? P.elapsedLv((perf.value.avg_ms || 0) / 1000) : null,
          color: "#b91c1c", icon: "clock" });
        return list;
      });
      const byModel = computed(() => {
        const rows = sum.value.by_model || [];
        const total = rows.reduce((a, r) => a + (r.tokens || 0), 0) || 1;
        return rows.map((r) => Object.assign({}, r,
          { pct: Math.round(((r.tokens || 0) / total) * 100) }));
      });
      const cols = [
        { title: "模型", key: "model", render: (r) => modelChip(r.model) },
        { title: "请求", key: "reqs", align: "right", width: 80,
          render: (r) => h("span", { style: MONO }, nf(r.reqs)) },
        { title: "Tokens", key: "tokens", align: "right", width: 110,
          render: (r) => h("span", { style: MONO }, nf(r.tokens)) },
        /* 百分数放条外:6px 高的条塞不进文字,inside 会把标签压在条上。 */
        { title: "占比", key: "pct", width: 132, render: (r) =>
            h(naive.NProgress, { type: "line", percentage: r.pct, height: 6,
              borderRadius: 99, "indicator-placement": "outside",
              style: "min-width:96px" }, () =>
              h("span", { style: Object.assign({ fontSize: "11.5px" }, MONO) },
                r.pct + "%")) },
      ];
      const quota = computed(() => {
        const u = o.value.usage;
        if (!u) return [];
        const unit = o.value.limit_unit === "tokens" ? "Tokens" : "次";
        return [["今日", u.daily], ["本周", u.weekly], ["本月", u.monthly]]
          .filter((x) => x[1] && x[1].limit > 0)
          .map((x) => ({ label: x[0], used: x[1].used, limit: x[1].limit, unit: unit,
            pct: Math.min(100, Math.round((x[1].used / x[1].limit) * 100)) }));
      });

      /* 套餐卡要显示完整:分组、状态、计费、倍率、并发、限额口径、
         可用模型、余额/累计消费。原先只有 4 行,可用模型与余额都看不到。 */
      const planRows = computed(() => {
        const rows = [["分组", o.value.group || "未分配"],
          ["计费方式", POLICY[policy.value] || policy.value]];
        if (o.value.rate_multiplier != null)
          rows.push(["倍率", "×" + o.value.rate_multiplier]);
        rows.push(["并发上限",
          o.value.concurrency_limit ? o.value.concurrency_limit + " 个请求" : "不限"]);
        rows.push(["限额口径",
          o.value.limit_unit === "tokens" ? "Token" : "请求次数"]);
        return rows;
      });
      const models = computed(() => o.value.supported_models || []);

      return Object.assign({ cards, byModel, cols, quota, o, policy, sum,
        checkin, checkingIn, doCheckin,
        planRows, models, keys, nf, kf, fmt, MONO,
        go: P.go }, s);
    },
    template: `
<Load :loading="loading" :error="error" :on-retry="reload">
<div class="console-page-stack">
<section class="console-hero">
  <div class="console-hero-copy">
    <div class="console-eyebrow">{{ cards[0].label }}</div>
    <div class="console-hero-title"><MetricValue :value="o.group ? cards[0].value : '未分配分组'"/></div>
    <p class="console-hero-note">{{ o.group ? cards[0].sub : '联系管理员分配分组，或查看可购买的套餐。' }}</p>
    <div class="console-hero-actions">
      <n-button type="primary" @click="go('#/keys')">管理 API 密钥</n-button>
      <n-button secondary @click="go(policy === 'balance' ? '#/wallet' : '#/billing')">
        {{ policy === 'balance' ? '充值余额' : '查看计费与套餐' }}</n-button>
    </div>
  </div>
  <aside class="console-hero-aside">
    <div class="console-section-title">每日签到</div>
    <p class="console-section-note">
      <template v-if="checkin.checked_in">今日已领取 \${{ fmt(checkin.reward, 4) }}，明天再来。</template>
      <template v-else>今日可随机领取 \${{ fmt(checkin.range && checkin.range.min, 4) }}
        ～ \${{ fmt(checkin.range && checkin.range.max, 4) }}。</template>
    </p>
    <n-button secondary :loading="checkingIn" :disabled="!!checkin.checked_in" @click="doCheckin">
      {{ checkin.checked_in ? '今日已签到' : '立即签到' }}</n-button>
  </aside>
</section>
<section>
  <div class="console-section-heading">
    <div><h2 class="console-section-title">调用概况</h2>
      <p class="console-section-note">近 24 小时用量、累计消耗与实际响应表现。</p></div>
    <n-button text type="primary" @click="go('#/usage')">查看使用记录 →</n-button>
  </div>
<n-grid :cols="'1 340:2 560:3'" :x-gap="14" :y-gap="14" responsive="self">
  <n-gi v-for="c in cards.slice(2, 5)" :key="c.label">
    <StatCard v-bind="c"/>
  </n-gi>
</n-grid>
</section>

<n-grid :cols="'1 560:2 980:4'" :x-gap="14" :y-gap="14" responsive="self">
  <n-gi v-for="c in cards.slice(5)" :key="c.label"><StatCard v-bind="c"/></n-gi>
</n-grid>
<ModelCachePanel/>
<section>
  <div class="console-section-heading">
    <div><h2 class="console-section-title">继续你的工作</h2>
      <p class="console-section-note">{{ nf(keys.active) }} 把有效密钥 · {{ nf(keys.total) }} 把密钥总计 · 当前分组 {{ o.group || '未分配' }}。</p></div>
  </div>
  <div class="console-action-grid">
    <a class="console-action-card" href="#/keys"><Ic name="key" :size="22"/>
      <strong>接入应用</strong><span>创建密钥，复制 API 地址，配置使用限制。</span></a>
    <a class="console-action-card" href="#/plaza"><Ic name="cube" :size="22"/>
      <strong>探索模型</strong><span>按供应商和计费方式查找可用模型。</span></a>
    <a class="console-action-card" href="#/playground"><Ic name="response" :size="22"/>
      <strong>在线体验</strong><span>发起真实对话，查看回答与 token 用量。</span></a>
  </div>
</section>

<div class="console-workspace-grid">
  <div>
    <n-card title="今日模型分布" size="small">
      <n-data-table :columns="cols" :data="byModel" :bordered="false" size="small" :scroll-x="580"/>
      <template #footer v-if="!byModel.length">
        <n-text depth="3" style="font-size:12px">今日还没有调用记录。</n-text>
      </template>
    </n-card>
  </div>
  <div>
    <n-card size="small" :title="policy === 'quota' ? '额度用量与套餐' : '当前套餐'">
      <template #header-extra>
        <n-tag v-if="o.group" size="small" round :bordered="false" type="info">
          {{ o.group }}</n-tag>
        <n-tag v-else size="small" round :bordered="false">未分配</n-tag>
      </template>
      <n-space vertical :size="14">
        <div v-if="quota.length">
          <div v-for="q in quota" :key="q.label" style="margin-bottom:10px">
            <n-space justify="space-between" style="margin-bottom:4px">
              <n-text depth="3" style="font-size:12px">{{ q.label }}</n-text>
              <n-text :style="MONO" style="font-size:11.5px">
                {{ nf(q.used) }} / {{ nf(q.limit) }} {{ q.unit }}
              </n-text>
            </n-space>
            <n-progress type="line" :percentage="q.pct" :height="7"
              :show-indicator="false" :border-radius="99"
              :status="q.pct >= 90 ? 'error' : q.pct >= 60 ? 'warning' : 'success'"/>
          </div>
        </div>

        <n-descriptions :column="1" size="small" label-placement="left"
          :label-style="{opacity:.6,width:'72px'}">
          <n-descriptions-item v-for="r in planRows" :key="r[0]" :label="r[0]">
            <span :style="r[0] === '倍率' || r[0] === '并发上限' ? MONO : null">
              {{ r[1] }}</span>
          </n-descriptions-item>
          <n-descriptions-item v-if="policy === 'balance'" label="可用余额">
            <span :style="MONO" style="font-weight:650;color:#15803d">
              \${{ fmt(o.balance) }}</span>
          </n-descriptions-item>
          <n-descriptions-item v-if="policy === 'balance'" label="累计消费">
            <span :style="MONO">\${{ fmt(o.total_spent) }}</span>
          </n-descriptions-item>
        </n-descriptions>

        <div>
          <n-text depth="3" style="font-size:12px;display:block;margin-bottom:6px">
            模型授权范围 · {{ models.length }} 条规则
          </n-text>
          <n-space :size="4">
            <n-tag v-for="m in models.slice(0, 8)" :key="m" size="small" round
              :bordered="false" :style="MONO">{{ m }}</n-tag>
            <n-tooltip v-if="models.length > 8" placement="top">
              <template #trigger>
                <n-tag size="small" round :bordered="false" class="dtl">
                  +{{ models.length - 8 }}</n-tag>
              </template>
              <div :style="MONO" style="font-size:11.5px">
                <div v-for="m in models.slice(8)" :key="m">{{ m }}</div>
              </div>
            </n-tooltip>
            <n-text v-if="!models.length" depth="3" style="font-size:12px">
              未配置(该分组不允许任何模型)</n-text>
          </n-space>
        </div>

        <n-button text type="primary" size="small"
          @click="go('#/billing')">查看套餐与单价 →</n-button>
      </n-space>
    </n-card>
  </div>
</div>
</div>
</Load>`,
  };
  /* 密钥列的空态一律渲染「意思」而不是留白:留白会被读成「不允许」或
     「数据缺失」,而额度/模型/IP/过期这几列的空态各有确切含义。 */
  const muted = (txt) => h(naive.NText, { depth: 3,
    style: { fontSize: "12px" } }, () => txt);
  const monoTag = (txt, w) => h(naive.NTag, { size: "tiny", round: true,
    bordered: false }, () => h("span", { style: Object.assign(
      { fontSize: "10.5px", display: "inline-block", maxWidth: w,
        overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
        verticalAlign: "bottom" }, MONO) }, txt));
  /* 多项只报条数 + 悬浮看全部:白名单可以有十几条,逐个铺开会把列宽顶爆,
     两个 tag 加 +N 也已经溢出到下一列。列宽稳定比一眼看清更重要。 */
  const tagList = (items, empty, w, tip) => {
    if (!items || !items.length)
      return tip ? wrapTip(muted(empty), tip, "top") : muted(empty);
    const rows = items.map((x) => h("div", { style: MONO }, x));
    if (items.length === 1) return wrapTip(monoTag(items[0], w), rows, "top");
    return wrapTip(h("span", { class: "dtl", style: { fontSize: "12px" } },
      items.length + " 项"), rows, "top");
  };
  /* 额度是「本密钥消费上限」而非独立钱包:0 表示不限,此时只报已用。 */
  const quotaCell = (r) => {
    const q = r.quota || 0;
    const used = r.used_quota || 0;
    if (!q) return stack(muted("无限"), "已用 $" + fmt(used));
    const pct = Math.min(100, Math.round((used / q) * 100));
    const left = Math.max(0, q - used);
    return h("div", { style: { minWidth: 0 } }, [
      h("div", { style: Object.assign({ fontSize: "12.5px", fontWeight: 600,
        color: left <= 0 ? "#b91c1c" : undefined }, MONO) },
        "$" + fmt(left) + " / $" + fmt(q)),
      h(naive.NProgress, { type: "line", percentage: pct, height: 4,
        showIndicator: false, borderRadius: 99, style: { marginTop: "3px" },
        status: pct >= 100 ? "error" : pct >= 80 ? "warning" : "success" }),
    ]);
  };

  /* 抽屉里的分节标题:柔和色底图标块 + 标题 + 一行说明,右侧可挂折叠箭头。
     和顶部 StatCard 同一套视觉语言,不另造一种。 */
  const sectionHead = (icon, title, sub, color, extra) => h("div", {
    style: { display: "flex", alignItems: "center", gap: "11px" } }, [
    h("span", { style: { display: "grid", placeItems: "center", width: "34px",
      height: "34px", borderRadius: "10px", flex: "0 0 auto",
      background: color + "1e" } }, ic(icon, 17, color, 1.8)),
    h("div", { style: { flex: 1, minWidth: 0 } }, [
      h("div", { style: { fontSize: "13.5px", fontWeight: 650,
        letterSpacing: "-.01em" } }, title),
      h("div", { style: { fontSize: "11.5px", opacity: 0.52,
        marginTop: "1px" } }, sub),
    ]),
    extra || null,
  ]);
  const SectionHead = { props: ["icon", "title", "sub", "color"],
    render() { return sectionHead(this.icon, this.title, this.sub, this.color,
      this.$slots.extra ? this.$slots.extra() : null); } };

  const Keys = {
    components: { Load, SectionHead, Ic, StatCard },
    setup() {
      const rows = ref([]);
      const grp = reactive({ name: "", models: [] });
      const groupOpts = computed(() => {
        const name = grp.name || "未分配";
        return [{ label: name, value: name }];
      });
      const stats = ref({ total: 0, active: 0, cost_today: 0, cost_all: 0 });
      const s = useFetch(() => api("GET", "/keys").then((r) => {
        rows.value = r.keys || [];
        grp.name = r.group || "";
        grp.models = r.available_models || [];
        stats.value = r.stats || stats.value;
      }));
      const created = ref(null);
      /* 创建与编辑共用一个抽屉:字段完全相同,拆两套只会让「新建能配的项
         编辑里没有」这类偏差慢慢长出来。mode 决定标题、按钮字与是否显示数量。 */
      const dr = reactive({ show: false, mode: "create", id: 0,
        name: "", count: 1, exp: null, unlimited: true, quota: 0,
        models: [], ipText: "", adv: false,
        opts: [], loading: false, saving: false });
      const cc = reactive({ show: false, app: "claude", name: "bit-api",
        key: "", models: {}, options: [], loading: false });

      const patch = (row, body, okText) =>
        api("PATCH", "/keys/" + row.id, body)
          .then(() => { msg.success(okText); return s.reload(); })
          .catch((e) => msg.error(e.message));

      /* 完整密钥不进列表响应,要用时按 id 单取,取到后本地缓存;
         并发点同一行只发一次请求。 */
      const full = reactive({});
      const inflight = {};
      function reveal(id) {
        if (full[id]) return Promise.resolve(full[id]);
        if (!inflight[id]) {
          inflight[id] = api("GET", "/keys/" + id + "/reveal")
            .then((r) => { full[id] = r.key; return r.key; })
            .then((k) => { delete inflight[id]; return k; },
              (e) => { delete inflight[id]; throw e; });
        }
        return inflight[id];
      }
      /* 复制成功的那一行短暂换成对勾:连点几行时挨个弹 toast 比不给反馈更烦。 */
      const copied = reactive({});
      function copyKey(row) {
        reveal(row.id).then((k) => {
          copy(k, "已复制完整密钥");
          copied[row.id] = true;
          setTimeout(() => { delete copied[row.id]; }, 1600);
        }).catch((e) => msg.error(e.message));
      }

      const RESET = { id: 0, name: "", count: 1, exp: null, unlimited: true,
        quota: 0, models: [], ipText: "", adv: false, opts: [],
        loading: false, saving: false };
      const keyModelOpts = () => grp.models.map((m) => ({ label: m, value: m }));
      /* 新建与编辑都使用账户当前分组展开后的模型清单。不能用待编辑密钥去查:
         它只能看见自己已经收紧后的范围,会导致用户无法重新放宽限制。 */
      function openCreate() {
        Object.assign(dr, RESET, { show: true, mode: "create",
          opts: keyModelOpts() });
      }
      function openEdit(row) {
        const q = row.quota || 0;
        Object.assign(dr, RESET, { show: true, mode: "edit", id: row.id,
          name: row.name || "",
          exp: row.expires_at ? row.expires_at * 1000 : null,
          unlimited: !q, quota: q,
          models: (row.allowed_models || []).slice(),
          ipText: (row.allowed_ips || []).join("\n"),
          adv: !!((row.allowed_models || []).length ||
            (row.allowed_ips || []).length),
          opts: keyModelOpts() });
      }
      function save() {
        const ips = dr.ipText.split(/[\n,;，；\s]+/)
          .map((x) => x.trim()).filter(Boolean);
        const body = { name: dr.name,
          quota: dr.unlimited ? 0 : (Number(dr.quota) || 0),
          allowed_models: dr.models, allowed_ips: ips };
        dr.saving = true;
        const done = (r) => {
          dr.show = false;
          if (dr.mode === "create") {
            const list = r.keys || [];
            created.value = list;
            if (list.length === 1) copy(list[0].key, "已创建并复制到剪贴板");
            else msg.success("已创建 " + list.length + " 把密钥");
          } else msg.success("已保存");
          return s.reload();
        };
        let p;
        if (dr.mode === "create") {
          body.count = dr.count || 1;
          if (dr.exp) body.expires_at = Math.floor(dr.exp / 1000);
          p = api("POST", "/keys", body);
        } else {
          /* 编辑必须显式传 0,否则「取消过期」这个动作传不过去。 */
          body.expires_at = dr.exp ? Math.floor(dr.exp / 1000) : 0;
          p = api("PATCH", "/keys/" + dr.id, body);
        }
        p.then(done).catch((e) => msg.error(e.message))
          .then(() => { dr.saving = false; });
      }
      /* 过期时间快捷档:点一下就把绝对时间算好填进 date-picker,
         用户仍能再手改具体到分钟。 */
      const setExp = (sec) => { dr.exp = sec ? Date.now() + sec * 1000 : null; };

      function remove(row) {
        dlg.warning({
          title: "删除密钥", positiveText: "删除", negativeText: "取消",
          content: "使用「" + (row.name || row.key_masked) +
            "」的应用会立即失效,且无法恢复。",
          onPositiveClick: () => api("DELETE", "/keys/" + row.id)
            .then(() => { msg.success("已删除"); return s.reload(); })
            .catch((e) => msg.error(e.message)),
        });
      }

      function openCC(row) {
        cc.app = "claude";
        cc.models = {};
        cc.name = CCSWITCH_APPS.claude.defaultName;
        cc.key = "";
        cc.show = true;
        cc.loading = true;
        reveal(row.id).then((k) => {
          cc.key = k;
          return ccLoadModels(k);
        }).then((list) => {
          cc.options = list.map((m) => ({ label: m, value: m }));
          cc.loading = false;
        }, (e) => {
          cc.loading = false;
          cc.show = false;
          msg.error(e.message);
        });
      }
      /* 换应用要清模型:codex 的 model 放到 claude 的 opusModel 上毫无意义。 */
      watch(() => cc.app, (a) => {
        cc.models = {};
        cc.name = (CCSWITCH_APPS[a] || {}).defaultName || "bit-api";
      });
      const ccFields = computed(() =>
        (CCSWITCH_APPS[cc.app] || CCSWITCH_APPS.claude).fields);
      function fireCC() {
        const need = ccFields.value.filter((f) => f[2] && !cc.models[f[0]]);
        if (need.length) return msg.warning("请填写" + need[0][1]);
        if (!cc.name.trim()) return msg.warning("请填写名称");
        window.open(buildCCSwitchURL(cc.app, cc.name.trim(), cc.models, cc.key),
          "_blank");
        cc.show = false;
        msg.success("已唤起 CC Switch;若无反应请确认已安装该客户端");
      }

      /* 状态判定只有一处:列里显示什么,筛选就按什么筛。
         优先级 人工禁用 → 过期 → 额度耗尽 —— 禁用是用户自己的动作,最该先
         告诉他;其余两种是被动失效。两边各写一套的话,一把既禁用又额度耗尽的
         密钥列里写「已禁用」、筛「额度耗尽」却搜得出来,用户会以为筛错了。 */
      const effStatus = (r) => (r.status !== "active" ? "disabled"
        : r.expired ? "expired" : r.quota_exhausted ? "exhausted" : "active");

      /* 十列顺序照用户指定。分组/可用模型每行同值(密钥不覆盖分组),
         照样列出以便和 new-api 的形态对齐,表尾注明来源。
         列宽逐列压到「内容刚好放得下」,合计 1112px:1440 视口 + 展开侧栏时
         正好不横向滚动。名称与操作仍两端钉住,窄屏滚动时认得出行、点得到按钮。 */
      const cols = [
        { title: "名称", key: "name", width: 112, fixed: "left",
          ellipsis: { tooltip: true },
          render: (r) => r.name || muted("未命名") },
        { title: "状态", key: "status", width: 86, render: (r) => {
            const k = effStatus(r);
            return k === "exhausted" ? tag("warning", "额度耗尽")
              : tag(st(k)[0], st(k)[1]); } },
        /* 表头写「剩余 / 总额度」而不是「剩余额度 / 总额度」:后者在 126px 下
           会折成两行,把整行表头顶高。 */
        { title: "剩余 / 总额度", key: "quota", width: 126, render: quotaCell },
        { title: "分组", key: "group", width: 68, render: () =>
            (grp.name ? tag("info", grp.name) : muted("未分配")) },
        /* 只显示脱敏串,完整明文点复制按钮时按 id 单取。
           表格是截图/投屏露得最多的界面,一次露的是所有行;
           而且 67 字符全文要吃掉近 430px,把其余九列挤成横向滚动。 */
        { title: "密钥", key: "key_masked", width: 154, render: (r) =>
            h(naive.NSpace, { size: 2, wrap: false, align: "center" }, () => [
              h("code", { style: Object.assign({ fontSize: "11.5px",
                whiteSpace: "nowrap", opacity: 0.82 }, MONO) }, r.key_masked),
              wrapTip(h(naive.NButton, { size: "tiny", quaternary: true,
                onClick: () => copyKey(r), "aria-label": "复制完整密钥" },
                () => ic(copied[r.id] ? "check" : "copy", 13,
                  copied[r.id] ? "#15803d" : undefined)),
                [copied[r.id] ? "已复制" : "复制完整密钥"], "top"),
            ]) },
        { title: "可用模型", key: "allowed_models", width: 94, render: (r) =>
            tagList(r.allowed_models, "跟随分组", "74px",
              (grp.models.length ? grp.models : ["无"]).map(
                (m) => h("div", { style: MONO }, m))) },
        { title: "IP 限制", key: "allowed_ips", width: 90, render: (r) =>
            tagList(r.allowed_ips, "无限制", "70px") },
        { title: "创建时间", key: "created_at", width: 90, render: (r) =>
            h("span", { style: { fontSize: "12px" } },
              absTime(r.created_at).slice(0, 10)) },
        { title: "最后使用", key: "last_used_at", width: 90, render: (r) => {
            const ts = r.last_used_at;
            if (!ts) return muted("从未使用");
            const zombie = (Date.now() / 1000 - ts) > 90 * 86400;
            return wrapTip(h("span", { class: "dtl", style: { fontSize: "12px",
              color: zombie ? "#a17a10" : undefined } }, relTime(ts)),
              [absTime(ts)], "top"); } },
        { title: "过期时间", key: "expires_at", width: 90, render: (r) =>
            (r.expires_at
              ? h("span", { style: { fontSize: "12px",
                  color: r.expired ? "#b91c1c" : undefined } },
                absTime(r.expires_at).slice(0, 10))
              : muted("永不过期")) },
        /* 四个动作换成图标:文字版要 232px,是这一列宽度的主要来源。
           每个都带 aria-label 与悬浮文字,图标本身只承担认形状。 */
        { title: "操作", key: "act", width: 112, fixed: "right", align: "center",
          render: (r) => {
            const off = r.status === "active";
            const btn = (icon, tip, color, onClick) => wrapTip(
              h(naive.NButton, { size: "tiny", quaternary: true, onClick: onClick,
                "aria-label": tip }, () => ic(icon, 14, color)), [tip], "top");
            return h(naive.NSpace, { size: 0, wrap: false, justify: "center" },
              () => [
                btn("link", "导入到 CC Switch", undefined, () => openCC(r)),
                btn("pencil", "编辑", undefined, () => openEdit(r)),
                btn(off ? "ban" : "check", off ? "禁用" : "启用",
                  off ? undefined : "#15803d",
                  () => patch(r, { status: off ? "disabled" : "active" },
                    off ? "已禁用" : "已启用")),
                btn("trash", "删除", "#b91c1c", () => remove(r)),
              ]);
          } },
      ];
      /* 筛选在前端做:密钥总量是个人级(上限 50 把/次创建),不值得为它加
         后端查询参数。搜索同时匹配名称与脱敏串,因为用户手里往往只有尾号。 */
      const q = ref("");
      const fstatus = ref("");
      const view = computed(() => {
        const kw = q.value.trim().toLowerCase();
        return rows.value.filter((r) => {
          if (kw && (r.name || "").toLowerCase().indexOf(kw) < 0 &&
            (r.key_masked || "").toLowerCase().indexOf(kw) < 0) return false;
          return !fstatus.value || effStatus(r) === fstatus.value;
        });
      });
      const statusOpts = [{ label: "全部状态", value: "" },
        { label: "启用", value: "active" }, { label: "已禁用", value: "disabled" },
        { label: "已过期", value: "expired" },
        { label: "额度耗尽", value: "exhausted" }];
      const EXP_QUICK = [["永不", 0], ["1 小时", 3600], ["1 天", 86400],
        ["1 个月", 30 * 86400], ["1 年", 365 * 86400]];

      /* 概览四卡:总数/活跃/今日费用/累计费用。活跃 = 排除禁用、过期、额度耗尽
         三种失效,与状态列同一套判定,不是简单数 status==active。
         费用走 fmt 的自适应精度而非固定 4 位:大额显示 $104.32 不刺眼,
         小额自动给 6 位,否则一笔 $0.0002 的消费会显示成 $0.00 像没扣钱。 */
      const cards = computed(() => [
        { label: "密钥总数", value: nf(stats.value.total), icon: "key",
          color: "#4a6fa5",
          sub: view.value.length === stats.value.total ? "含已禁用与已过期"
            : "当前筛选 " + view.value.length + " 条" },
        { label: "活跃密钥", value: nf(stats.value.active), icon: "check",
          color: "#15803d", tint: true,
          sub: stats.value.total - stats.value.active > 0
            ? (stats.value.total - stats.value.active) + " 把已失效" : "全部可用" },
        { label: "今日费用", value: "$" + fmt(stats.value.cost_today),
          icon: "arrowUp", color: "#3f6b8a", sub: "近 24 小时实扣" },
        { label: "累计费用", value: "$" + fmt(stats.value.cost_all),
          icon: "cube", color: "#6b5b95", sub: "全部密钥历史合计" },
      ]);

      return Object.assign({ rows, view, cols, created, dr, save, openCreate,
        cc, ccFields, fireCC, copy, grp, groupOpts, q, fstatus, statusOpts, cards,
        setExp, EXP_QUICK, MONO: MONO, endpoint: API_ENDPOINT,
        copyEndpoint: () => copy(API_ENDPOINT, "已复制 API 端点"),
        pagination: computed(() => ({ pageSize: 20, itemCount: view.value.length,
          showSizePicker: true, pageSizes: [10, 20, 50],
          prefix: (p) => "显示 " + (p.itemCount ? p.startIndex + 1 : 0) + " 至 " +
            Math.min(p.endIndex + 1, p.itemCount) + " 共 " + p.itemCount + " 条" })),
        CCSWITCH_APPS: CCSWITCH_APPS, base: ccServerAddress() }, s);
    },
    template: `
<div class="console-page-stack">
<section class="console-workspace-grid">
  <n-card class="console-panel" size="small" title="连接你的应用">
    <template #header-extra><n-button type="primary" @click="openCreate">创建密钥</n-button></template>
    <p class="console-section-note">将 API 端点与一把密钥填入应用。已有密钥可在下方复制，或直接导入 CC Switch。</p>
    <n-form label-placement="top">
      <n-form-item label="API 端点" :show-feedback="false">
        <n-input-group>
          <n-input :value="endpoint" readonly :input-props="{'aria-label':'API 端点'}"/>
          <n-button secondary aria-label="复制 API 端点" @click="copyEndpoint">
            <template #icon><Ic name="copy" :size="15"/></template>复制</n-button>
        </n-input-group>
      </n-form-item>
    </n-form>
    <p class="console-section-note">需要让 AI 搜索、读取网页？<n-button tag="a" href="/mcp-guide" text type="primary" style="min-height:44px">查看 MCP 接入指南 →</n-button></p>
  </n-card>
  <n-card class="console-panel" size="small" title="访问范围">
    <div class="console-inline-stat"><span class="console-inline-stat-label">当前分组</span>
      <strong class="console-inline-stat-value">{{ grp.name || '未分配' }}</strong></div>
    <p class="console-section-note">分组提供 {{ grp.models.length }} 个可选模型。每把密钥可以进一步设置额度、模型及 IP 限制。</p>
    <a href="#/billing">查看分组计费规则 →</a>
  </n-card>
</section>
<div class="console-resource-strip">
  <div class="console-inline-stat" v-for="c in cards" :key="c.label">
    <span class="console-inline-stat-label">{{ c.label }}</span>
    <strong class="console-inline-stat-value"><MetricValue :value="c.value"/></strong>
    <span class="console-section-note">{{ c.sub }}</span>
  </div>
</div>
<section class="console-table-card">
  <div class="console-section-heading">
    <div><h2 class="console-section-title">我的密钥</h2>
      <p class="console-section-note">名称用于识别应用；密钥只显示首尾片段，复制时获取完整值。</p></div>
    <n-button secondary :loading="loading" @click="reload">刷新</n-button>
  </div>
  <div class="console-toolbar">
    <n-input v-model:value="q" clearable placeholder="搜索名称或密钥尾号…"
      style="width:250px">
      <template #prefix><Ic name="search" :size="14"/></template>
    </n-input>
    <n-select v-model:value="fstatus" :options="statusOpts" style="width:148px"/>
    <n-text depth="3">{{ view.length }} 条匹配密钥</n-text>
  </div>
  <Load :loading="loading" :error="error" :on-retry="reload">
    <n-data-table :columns="cols" :data="view" :bordered="false" size="small"
      :scroll-x="1112" :row-key="(r) => r.id" :pagination="pagination">
      <template #empty><n-empty :description="q || fstatus ? '没有匹配的密钥，请调整筛选条件' : '还没有密钥，创建后即可接入应用'"/></template>
    </n-data-table>
  </Load>
</section>
<p class="console-section-note">密钥不能更改所属分组。「可用模型」为空时跟随分组白名单，悬停可查看完整范围。</p>
</div>
<n-drawer v-model:show="dr.show" width="min(520px, 100vw)" placement="right">
  <n-drawer-content closable :native-scrollbar="false"
    :title="dr.mode === 'create' ? '创建 API 密钥' : '编辑 API 密钥'">
    <n-text depth="3" style="font-size:12.5px;display:block;margin:-4px 0 4px">
      {{ dr.mode === 'create' ? '通过提供必要信息添加新的 API 密钥。'
        : '修改这把密钥的名称、额度与访问限制。' }}
    </n-text>

    <n-divider style="margin:16px 0 14px"/>
    <SectionHead icon="key" title="基本信息"
      :sub="dr.mode === 'create' ? '名称、过期时间与创建数量' : '名称与过期时间'"
      color="#4a6fa5"/>
    <n-form label-placement="top" style="margin-top:14px">
      <n-form-item label="名称" :show-feedback="false" style="margin-bottom:14px">
        <n-input v-model:value="dr.name" placeholder="输入名称(可留空)" clearable/>
      </n-form-item>
      <n-form-item label="分组" :show-feedback="false" style="margin-bottom:14px">
        <n-select :value="grp.name || '未分配'" :options="groupOpts" disabled/>
        <template #feedback></template>
      </n-form-item>
      <n-text depth="3" style="font-size:11px;display:block;margin:-8px 0 14px">
        分组由管理员分配,决定倍率与可用模型范围,密钥自身不能改。
      </n-text>
      <n-form-item label="过期时间" :show-feedback="false" style="margin-bottom:6px">
        <n-space :size="8" align="center" style="width:100%">
          <n-date-picker v-model:value="dr.exp" type="datetime" clearable
            style="width:228px" placeholder="永不过期"/>
          <n-button v-for="e in EXP_QUICK" :key="e[0]" size="small" secondary
            @click="setExp(e[1])">{{ e[0] }}</n-button>
        </n-space>
      </n-form-item>
      <n-text depth="3" style="font-size:11px;display:block;margin:6px 0 14px">
        快捷档只是把绝对时间算好填进左边,仍可再手改到具体分钟。
      </n-text>
      <n-form-item v-if="dr.mode === 'create'" label="数量" :show-feedback="false"
        style="margin-bottom:6px">
        <n-input-number v-model:value="dr.count" :min="1" :max="50"
          style="width:100%"/>
      </n-form-item>
      <n-text v-if="dr.mode === 'create'" depth="3"
        style="font-size:11px;display:block;margin-top:6px">
        一次性创建多个 API 密钥(名称将添加随机后缀)。
      </n-text>
    </n-form>

    <n-divider style="margin:18px 0 14px"/>
    <SectionHead icon="wallet" title="额度设置" sub="这把密钥的累计消费上限"
      color="#15803d"/>
    <div style="display:flex;align-items:center;gap:12px;margin-top:14px">
      <div style="flex:1;min-width:0">
        <div style="font-size:13px">无限额度</div>
        <div style="font-size:11.5px;opacity:.52;margin-top:2px">
          关闭后可为这把密钥单独设置消费上限</div>
      </div>
      <n-switch v-model:value="dr.unlimited"/>
    </div>
    <n-form v-if="!dr.unlimited" label-placement="top" style="margin-top:14px">
      <n-form-item label="额度上限(美元)" :show-feedback="false"
        style="margin-bottom:6px">
        <n-input-number v-model:value="dr.quota" :min="0" :precision="4"
          style="width:100%"/>
      </n-form-item>
    </n-form>
    <n-text depth="3" style="font-size:11px;display:block;margin-top:8px">
      额度只是这把密钥的消费上限,扣的仍是账户余额,不是独立钱包。
    </n-text>

    <n-divider style="margin:18px 0 14px"/>
    <SectionHead icon="gear" title="高级设置" sub="设置这把密钥的访问限制"
      color="#71717a">
      <template #extra>
        <n-button quaternary size="tiny" @click="dr.adv = !dr.adv"
          :aria-label="dr.adv ? '收起高级设置' : '展开高级设置'">
          <Ic :name="dr.adv ? 'chevronUp' : 'chevronDown'" :size="15"/>
        </n-button>
      </template>
    </SectionHead>
    <n-collapse-transition :show="dr.adv">
      <n-form label-placement="top" style="margin-top:14px">
        <n-form-item label="模型限制" :show-feedback="false"
          style="margin-bottom:6px">
          <n-select v-model:value="dr.models" multiple filterable tag clearable
            :loading="dr.loading" :options="dr.opts"
            placeholder="选择模型(留空表示允许所有)" :max-tag-count="3"/>
        </n-form-item>
        <n-text depth="3" style="font-size:11px;display:block;margin:6px 0 14px">
          留空 = 跟随分组白名单。这里只能收紧:模型仍需在分组范围内,
          填了分组没授权的模型不会因此放行。
        </n-text>
        <n-form-item label="IP 白名单(支持 CIDR)" :show-feedback="false"
          style="margin-bottom:6px">
          <n-input v-model:value="dr.ipText" type="textarea" :rows="3"
            placeholder="每行一个 IP(留空表示无限制)"/>
        </n-form-item>
        <n-text depth="3" style="font-size:11px;display:block;margin-top:6px">
          请勿过度信任此功能,IP 可能被伪造,请配合 nginx、CDN 等网关使用。
        </n-text>
      </n-form>
    </n-collapse-transition>

    <template #footer>
      <n-space justify="end">
        <n-button @click="dr.show = false">关闭</n-button>
        <n-button type="primary" :loading="dr.saving" @click="save">
          {{ dr.mode === 'create' ? '创建密钥' : '保存更改' }}</n-button>
      </n-space>
    </template>
  </n-drawer-content>
</n-drawer>

<n-modal :show="!!created" preset="card" title="新密钥已创建"
  style="max-width:560px" @update:show="created = null">
  <n-text depth="3" style="font-size:12.5px">
    关闭后列表只显示首尾片段,要完整明文时点该行的复制按钮即可再取。</n-text>
  <n-space vertical :size="8" style="margin-top:12px">
    <n-input-group v-for="k in created" :key="k.id">
      <n-input :value="k.key" readonly/>
      <n-button secondary @click="copy(k.key, '已复制')">复制</n-button>
    </n-input-group>
  </n-space>
</n-modal>

<n-modal v-model:show="cc.show" preset="card" title="导入到 CC Switch"
  style="max-width:480px">
  <n-text depth="3" style="font-size:12.5px">
    通过 ccswitch:// 深链接把本站地址与该密钥直接导入本地 CLI 切换器。</n-text>
  <n-form label-placement="top" style="margin-top:14px">
    <n-form-item label="应用" :show-feedback="false" style="margin-bottom:14px">
      <n-radio-group v-model:value="cc.app">
        <n-radio-button value="claude">Claude Code</n-radio-button>
        <n-radio-button value="codex">Codex</n-radio-button>
        <n-radio-button value="gemini">Gemini CLI</n-radio-button>
      </n-radio-group>
    </n-form-item>
    <n-form-item label="名称" :show-feedback="false" style="margin-bottom:14px">
      <n-input v-model:value="cc.name"/></n-form-item>
    <n-form-item v-for="f in ccFields" :key="f[0]" :label="f[1]"
      :show-feedback="false" style="margin-bottom:14px">
      <n-select v-model:value="cc.models[f[0]]" filterable tag clearable
        :loading="cc.loading" :options="cc.options"
        :placeholder="f[2] ? '必填,可直接输入模型名' : '可留空'"/>
    </n-form-item>
    <n-form-item label="接入地址" :show-feedback="false">
      <n-input :value="cc.app === 'codex' ? base + '/v1' : base" readonly/>
    </n-form-item>
  </n-form>
  <template #footer>
    <n-space justify="end">
      <n-button @click="cc.show=false">取消</n-button>
      <n-button type="primary" @click="fireCC">打开 CC Switch</n-button>
    </n-space>
  </template>
</n-modal>`,
  };
  const Usage = {
    components: { Load, UsageMetrics: P.LiveUsageMetrics, Ic, StatCard },
    setup() {
      const rows = ref([]);
      const total = ref(0);
      const totals = ref({});
      const byModel = ref([]);
      const keyNames = ref({});
      const detail = ref(null);
      const range = ref("day");
      const page = ref(1);
      const pageSize = ref(20);
      const exporting = ref(false);
      const f = reactive({ span: null, model: "all", key: "all", failed: false });

      /* 列表、统计卡、导出三处用同一份条件:卡片说 40 次而翻页翻出 43 条,
         用户会先怀疑账。时间上自选区间优先,没选就按近 24 小时/7 天/30 天。 */
      const params = () => {
        const span = f.span && f.span.length === 2 ? f.span : null;
        return { range: range.value, model: f.model, api_key_id: f.key,
          since: span ? Math.floor(span[0] / 1000) : null,
          until: span ? Math.ceil(span[1] / 1000) : null,
          end_reason: f.failed ? "failed" : null };
      };
      const load = () => Promise.all([
        api("GET", "/usage" + qs(Object.assign(params(), {
          limit: pageSize.value, offset: (page.value - 1) * pageSize.value }))),
        api("GET", "/keys")]).then((r) => {
          const names = {};
          (r[1].keys || []).forEach((k) => { names[k.id] = k.name || ("#" + k.id); });
          keyNames.value = names;
          rows.value = (r[0].recent || []).map((x) => normUsage(x, names));
          total.value = r[0].total || 0;
          totals.value = r[0].totals || {};
          byModel.value = (r[0].summary || {}).by_model || [];
        });
      const s = useFetch(load);
      const query = () => { page.value = 1; return s.reload(); };
      watch(range, query);
      /* 改任一筛选条件立即重查,回到第一页:原先是本地筛、即改即见,换成服务端分页
         后不能让用户多学一步「改完要点查询」。区间只在选满两端时才算变化。 */
      watch(() => [f.model, f.key, f.failed,
        f.span && f.span.length === 2 ? f.span.join() : ""].join("|"), query);
      const jump = (n) => { page.value = n; s.reload(); };
      const resize = (n) => { pageSize.value = n; page.value = 1; s.reload(); };

      /* 模型下拉列的是这个时间窗里真用过的(summary.by_model),不是全站清单;
         当前选中的即便不在窗内也保留一项,否则换个窗口选项就消失、值却还在。 */
      const modelOpts = computed(() => {
        const seen = {};
        const list = [{ label: "全部模型", value: "all" }];
        byModel.value.forEach((m) => {
          if (m.model && !seen[m.model]) {
            seen[m.model] = 1;
            list.push({ label: m.model, value: m.model });
          }
        });
        if (f.model !== "all" && !seen[f.model])
          list.push({ label: f.model, value: f.model });
        return list;
      });
      const keyOpts = computed(() => [{ label: "全部密钥", value: "all" }]
        .concat(Object.keys(keyNames.value).map((id) =>
          ({ label: keyNames.value[id], value: String(id) }))));
      const reset = () => {
        f.span = null; f.model = "all"; f.key = "all"; f.failed = false;
        query();
      };
      const exportCsv = () => {
        exporting.value = true;
        download("/usage/export.csv" + qs(params()), "usage.csv")
          .then(() => msg.success("已导出当前条件下的记录(单次最多 10000 条)"))
          .catch((e) => msg.error(e.message))
          .then(() => { exporting.value = false; });
      };

      const cards = computed(() => {
        const a = totals.value;
        return [
          { label: "总请求数", value: nf(a.requests || 0), sub: "所选范围与筛选内",
            color: "#3f6b8a", icon: "doc" },
          { label: "总 Token", value: kf(a.tokens || 0),
            sub: "输入 " + kf(a.input_tokens || 0) + " / 输出 " +
              kf(a.output_tokens || 0) + " / 缓存 " + kf(a.cache_tokens || 0),
            color: "#a06a2c", icon: "cube" },
          { label: "所选范围消费", value: "$" + (a.actual_cost || 0).toFixed(4),
            sub: "长上下文 " + (a.long || 0) + " 次 · 失败 " + (a.failed || 0) + " 次",
            color: "#15803d", icon: "dollar", tone: "accent" },
          { label: "平均耗时", value: ((a.avg_ms || 0) / 1000).toFixed(2) + "s",
            sub: a.frt_ms == null ? "无首字记录"
              : "首字均 " + (a.frt_ms / 1000).toFixed(2) + "s",
            tone: a.requests ? P.elapsedLv((a.avg_ms || 0) / 1000) : null,
            color: "#6b5b95", icon: "clock" },
        ];
      });
      const cols = usageColumns((r) => { detail.value = r; });
      const rowClass = (r) => (!r.ok ? "row-err" : "");
      return Object.assign({ rows, cols, rowClass, cards, detail,
        range, f, modelOpts, keyOpts, reset, query, nf, usd, kf, fmt, MONO,
        tpsOf, C, dash, pxCell, absTime, END_TEXT, MODE, POLICY,
        total, page, pageSize, jump, resize, exportCsv, exporting,
        copyRid: () => detail.value && detail.value.rid
          ? copy(detail.value.rid, "已复制请求 ID") : msg.warning("该记录没有请求 ID"),
      }, s);
    },
    template: `
<div class="console-page-stack">
<div class="console-section-heading">
  <div><h2 class="console-section-title">追踪每一次调用</h2>
    <p class="console-section-note">按时间、模型和密钥查看用量；打开记录可核对响应表现与计费快照。</p></div>
  <n-button secondary :loading="exporting" @click="exportCsv">导出当前结果</n-button>
</div>
<n-grid :cols="'1 640:2 1180:4'" :x-gap="12" :y-gap="12" responsive="self"
        >
  <n-gi v-for="c in cards" :key="c.label">
    <StatCard :label="c.label" :value="c.value" :sub="c.sub" :color="c.color"
      :icon="c.icon" :tint="c.tint" :tone="c.tone"/>
  </n-gi>
</n-grid>

<n-card class="console-table-card" size="small" title="请求记录" :segmented="{content:true}"
        :content-style="'padding:14px 15px'">
  <n-grid :cols="'1 700:2 1100:5'" :x-gap="8" :y-gap="8" responsive="self">
    <n-gi span="1 700:2"><n-date-picker v-model:value="f.span" type="datetimerange"
      clearable size="small" style="width:100%"/></n-gi>
    <n-gi><n-select size="small" v-model:value="f.model" filterable
      :options="modelOpts"/></n-gi>
    <n-gi><n-select size="small" v-model:value="f.key" filterable
      :options="keyOpts"/></n-gi>
    <n-gi><n-checkbox v-model:checked="f.failed" size="small"
      style="height:28px;align-items:center">只看未正常结束</n-checkbox></n-gi>
  </n-grid>

  <n-space align="center" justify="space-between" :size="10" wrap
    style="margin-top:10px">
    <UsageMetrics/>
    <n-space :size="7" align="center">
      <n-radio-group v-model:value="range" size="small" :disabled="!!f.span">
        <n-radio-button value="day">近 24 小时</n-radio-button>
        <n-radio-button value="week">7 天</n-radio-button>
        <n-radio-button value="month">30 天</n-radio-button>
      </n-radio-group>
      <n-button size="small" secondary @click="reset">重置</n-button>
      <n-button size="small" type="primary" :loading="loading" @click="query">
        查询</n-button>
    </n-space>
  </n-space>

  <template #footer>
    <Load :loading="loading" :error="error" :on-retry="reload">
      <n-data-table class="usage-table" table-layout="fixed" :columns="cols" :data="rows" :bordered="false" size="small"
        :row-class-name="rowClass" :scroll-x="1280" :row-key="(r) => r.id"/>
      <n-space justify="space-between" align="center" style="margin-top:10px" wrap>
        <n-text depth="3" style="font-size:11.5px">
          自选时间区间优先于右上的近 24 小时 / 7 天 / 30 天;统计卡与列表始终是同一组条件。
        </n-text>
        <n-pagination class="usage-pagination" :page="page" :page-size="pageSize" :item-count="total"
          :page-slot="6" show-size-picker :page-sizes="[20,50,100,200]"
          @update:page="jump" @update:page-size="resize">
          <template #prefix>共 {{ nf(total) }} 条</template>
        </n-pagination>
      </n-space>
    </Load>
  </template>
</n-card>
</div>

<n-drawer class="usage-drawer" :show="!!detail" width="min(470px, 100vw)" placement="right"
  @update:show="v=>{ if(!v) detail=null }">
  <n-drawer-content v-if="detail" title="请求详情" closable
    :native-scrollbar="false">
    <n-space vertical :size="16">
      <n-alert v-if="!detail.ok" type="error" :bordered="false"
        :show-icon="false" title="请求未正常结束">
        {{ END_TEXT[detail.end] || detail.end }}
        <n-text depth="3" style="font-size:11.5px;display:block;margin-top:4px">
          网关未持久化上游错误原文,此处只显示结束原因。</n-text>
      </n-alert>

      <div>
        <n-text depth="3" style="font-size:12px;display:flex;align-items:center;gap:5px">
          <Ic name="doc" :size="13"/>基本信息</n-text>
        <n-descriptions :column="1" label-placement="left" size="small"
          :label-style="{opacity:.62,width:'82px',whiteSpace:'nowrap'}"
          style="margin-top:7px">
          <n-descriptions-item label="时间">
            <span :style="MONO">{{ absTime(detail.t) }}</span>
          </n-descriptions-item>
          <n-descriptions-item label="模型">
            <code :style="MONO">{{ detail.model }}</code></n-descriptions-item>
          <n-descriptions-item label="密钥">
            {{ detail.key }}
          </n-descriptions-item>
          <n-descriptions-item label="请求 ID">
            <n-text :style="MONO" style="font-size:12.5px">
              {{ dash(detail.rid) }}</n-text>
          </n-descriptions-item>
          <n-descriptions-item label="计量来源">
            <n-text :style="MONO" style="font-size:12.5px">
              {{ dash(detail.tokenSource) }}</n-text>
          </n-descriptions-item>
          <n-descriptions-item label="请求 IP">
            <span :style="MONO">{{ detail.ip || '未记录' }}</span>
          </n-descriptions-item>
        </n-descriptions>
      </div>
      <div>
        <n-text depth="3" style="font-size:12px;display:flex;align-items:center;gap:5px">
          <Ic name="cube" :size="13"/>计量</n-text>
        <n-descriptions :column="2" label-placement="top" size="small"
          :label-style="{opacity:.55,fontSize:'11px'}" style="margin-top:7px">
          <n-descriptions-item label="输入 token">
            <span :style="MONO" style="color:#4d7c5f;font-weight:650">
              {{ nf(detail.i) }}</span></n-descriptions-item>
          <n-descriptions-item label="输出 token">
            <span :style="MONO" style="color:#6b5b95;font-weight:650">
              {{ nf(detail.o) }}</span></n-descriptions-item>
          <n-descriptions-item label="缓存读取 token">
            <span :style="MONO" style="color:#3f6b8a;font-weight:650">
              {{ detail.cacheRead == null ? '—' : nf(detail.cacheRead) }}</span>
          </n-descriptions-item>
          <n-descriptions-item label="缓存写入 token">
            <span :style="MONO" style="color:#97882f;font-weight:650">
              {{ detail.cacheWrite == null ? '—' : nf(detail.cacheWrite) }}</span>
          </n-descriptions-item>
          <n-descriptions-item label="判定量 ctx">
            <span :style="MONO" style="color:#4a6fa5;font-weight:650">
              {{ detail.ctx == null ? '—' : nf(detail.ctx) }}</span>
            <n-text depth="3" v-if="detail.thr" style="font-size:11px">
              &nbsp;/ {{ nf(detail.thr) }}</n-text>
          </n-descriptions-item>
          <n-descriptions-item v-if="detail.cacheWrite30m != null" label="写入 · 30 分钟 token">
            {{ nf(detail.cacheWrite30m) }}</n-descriptions-item>
          <n-descriptions-item v-if="detail.cacheWrite12h != null" label="写入 · 12 小时 token">
            {{ nf(detail.cacheWrite12h) }}</n-descriptions-item>
          <n-descriptions-item v-if="detail.cacheWriteUnclassified" label="写入 · 未分档 token">
            {{ nf(detail.cacheWriteUnclassified) }}</n-descriptions-item>
          <n-descriptions-item v-if="detail.cacheWriteTTLValid === false" label="写入分档">
            上游分档不完整或合计不符，按合计和基础写入价结算</n-descriptions-item>
          <n-descriptions-item label="总耗时">
            <span :style="MONO" style="font-weight:650">
              {{ (detail.ms/1000).toFixed(2) }}s</span></n-descriptions-item>
          <n-descriptions-item label="首字 / 速度">
            <span :style="MONO" style="font-weight:650">{{ detail.frt == null
              ? '—' : (detail.frt/1000).toFixed(2) + 's' }}</span>
            <n-text depth="3" style="font-size:11px">
              &nbsp;· {{ tpsOf(detail) }} tok/s</n-text>
          </n-descriptions-item>
        </n-descriptions>
        <n-text depth="3" v-if="!detail.cacheSplit"
          style="font-size:11px;display:block;margin-top:5px">
          此记录未保存缓存读写明细。<template v-if="detail.cache != null">
            缓存合计 {{ nf(detail.cache) }} token。</template>
        </n-text>
      </div>

      <div>
        <n-text depth="3" style="font-size:12px;display:flex;align-items:center;gap:5px">
          <Ic name="tag" :size="13"/>定价快照</n-text>
        <n-descriptions :column="1" label-placement="left" size="small"
          :label-style="{opacity:.62,width:'82px',whiteSpace:'nowrap'}"
          style="margin-top:7px">
          <n-descriptions-item label="计费类型">
            {{ POLICY[detail.policy] || dash(detail.policy) }}
          </n-descriptions-item>
          <n-descriptions-item label="计费模式">
            {{ MODE[detail.mode] || detail.mode }}</n-descriptions-item>
          <n-descriptions-item label="档位" v-if="detail.px">
            <n-tag size="small" round :bordered="false"
              :type="detail.tier === 'long' ? 'warning' : 'default'">
              {{ detail.tier === 'long' ? '长上下文档' : '标准档' }}</n-tag>
          </n-descriptions-item>
          <template v-if="detail.px">
            <n-descriptions-item label="输入单价">
              <span :style="MONO">{{ pxCell(detail.px.i) }} /M</span>
            </n-descriptions-item>
            <n-descriptions-item label="输出单价">
              <span :style="MONO">{{ pxCell(detail.px.o) }} /M</span>
            </n-descriptions-item>
            <n-descriptions-item label="缓存读价" v-if="detail.px.cr != null">
              <span :style="MONO">{{ pxCell(detail.px.cr) }} /M</span>
            </n-descriptions-item>
            <n-descriptions-item label="缓存写价" v-if="detail.px.cw != null">
              <span :style="MONO">{{ pxCell(detail.px.cw) }} /M</span>
            </n-descriptions-item>
            <n-descriptions-item label="写入 · 30 分钟价" v-if="detail.px.cw30 != null">
              <span :style="MONO">{{ pxCell(detail.px.cw30) }} /M</span></n-descriptions-item>
            <n-descriptions-item label="写入 · 12 小时价" v-if="detail.px.cw12 != null">
              <span :style="MONO">{{ pxCell(detail.px.cw12) }} /M</span></n-descriptions-item>
          </template>
          <n-descriptions-item label="按次单价" v-else-if="detail.perReq">
            <span :style="MONO">{{ pxCell(detail.perPrice) }} × {{
              detail.units || 1 }} 次</span>
          </n-descriptions-item>
          <n-descriptions-item label="单价" v-else>
            <n-text depth="3">免费策略 · 无单价快照</n-text>
          </n-descriptions-item>
          <n-descriptions-item label="分组倍率">
            <span :style="MONO">{{ detail.ratio == null
              ? '—' : detail.ratio + 'x' }}</span></n-descriptions-item>
          <n-descriptions-item label="原价">
            <span :style="MONO">{{ usd(detail.list) }}</span>
          </n-descriptions-item>
          <n-descriptions-item label="实际扣费">
            <span :style="MONO" style="color:#a8613c;font-weight:700">
              {{ detail.free ? '免费' : usd(detail.cost) }}</span>
          </n-descriptions-item>
          <n-descriptions-item label="价目来源">
            <n-text :style="MONO" style="font-size:12.5px">
              {{ dash(detail.source) }}</n-text></n-descriptions-item>
        </n-descriptions>
        <n-text depth="3" style="font-size:11px;display:block;margin-top:6px;line-height:1.55">
          判定量 ctx = 输入 + 缓存写 + 缓存读(不含输出);严格大于阈值才跳档,
          整次 token 全部换用长档单价。快照在请求当时冻结,后续改价不影响这条账。
        </n-text>
      </div>
    </n-space>

    <template #footer>
      <n-space :size="8">
        <n-button size="small" secondary @click="copyRid">复制请求 ID</n-button>
        <n-button size="small" quaternary @click="detail=null">关闭</n-button>
      </n-space>
    </template>
  </n-drawer-content>
</n-drawer>`,
  };
  /* 流水来源的中文映射放在 portal-shared.js 的 reasonOf,管理台共用同一份。 */

  const Wallet = {
    components: { Load, StatCard },
    setup() {
      const bal = ref({ balance: 0, total_spent: 0 });
      const entries = ref([]);
      const orders = ref([]);
      const providers = ref([]);
      const inv = ref({ earned: 0 });
      const amount = ref(10);
      const provider = ref(null);
      const minTopup = ref(1);
      const maoRate = ref(1);
      const maoQuote = computed(() => {
        // 与两位美元金额、四位比例输入对应，避免浮点乘法把整数误向上进位。
        const cents = Math.round(Number(amount.value || 0) * 100);
        const rateUnits = Math.round(Number(maoRate.value) * 10000);
        return Math.ceil(cents * rateUnits / 1000000);
      });
      const code = ref("");
      const busy = reactive({ pay: false, redeem: false });
      // 付款窗口开着的那笔单。轮询它直到 completed —— 用户在新标签页付完款回来,
      // 这一页不会自己知道钱到了,而下单那一刻的 reload 必然还是 pending。
      const watching = ref(null);
      let timer = null;

      const stopWatch = () => { if (timer) { clearInterval(timer); timer = null; } };

      const s = useFetch(() => Promise.all([
        api("GET", "/ledger?limit=200"), api("GET", "/orders"),
        api("GET", "/payment/providers"), api("GET", "/invite"),
      ]).then((r) => {
        bal.value = r[0];
        entries.value = r[0].entries || [];
        orders.value = r[1].orders || [];
        providers.value = (r[2].providers || []).map((p) =>
          ({ label: p.display_name || p.name, value: p.name }));
        maoRate.value = r[2].mao_per_usd || 1;
        if (r[2].min_topup) {
          minTopup.value = r[2].min_topup;
          if (amount.value < r[2].min_topup) amount.value = r[2].min_topup;
        }
        if (!providers.value.some((p) => p.value === provider.value))
          provider.value = providers.value[0]?.value || null;
        inv.value = r[3];
      }));

      /* 轮询单笔订单。2 秒一跳、最多 5 分钟(150 跳) —— 支付宝扫码付款通常十几秒,
         封顶是为了用户忘了这个标签页时不要无限打接口。到账即停并刷新整页数据。 */
      const watchOrder = (otn) => {
        stopWatch();
        watching.value = { out_trade_no: otn, status: "pending" };
        let left = 150;
        timer = setInterval(() => {
          if (--left <= 0) { stopWatch(); watching.value = null; return; }
          api("GET", "/orders/" + encodeURIComponent(otn)).then((r) => {
            const o = r.order || {};
            watching.value = { out_trade_no: otn, status: o.status };
            if (o.status === "completed") {
              stopWatch();
              msg.success("到账 " + usd(o.amount));
              watching.value = null;
              s.reload();
            } else if (o.status === "failed") {
              stopWatch();
              watching.value = null;
              s.reload();
            }
          }).catch(() => {});   // 单次网络抖动不该中断轮询
        }, 2000);
      };

      onUnmounted(stopWatch);

      const pay = () => {
        if (!providers.value.some((p) => p.value === provider.value))
          return msg.warning("暂无可用支付方式，请稍后再试");
        if (!(amount.value >= minTopup.value))
          return msg.warning("单笔最低充值 " + usd(minTopup.value));
        busy.pay = true;
        api("POST", "/orders", { amount: amount.value, provider: provider.value })
          .then((r) => {
            const url = r.payment && r.payment.pay_url;
            const otn = r.order && r.order.out_trade_no;
            if (url) {
              window.open(url, "_blank", "noopener");
              msg.success("订单已创建,请在新窗口完成支付");
              if (otn) watchOrder(otn);   // 付完款回来这一页会自己更新
            } else msg.warning("渠道未返回支付地址");
            s.reload();
          }).catch((e) => msg.error(e.message))
          .then(() => { busy.pay = false; });
      };
      const doRedeem = () => {
        const c = code.value.trim();
        if (!c) return msg.warning("请输入兑换码");
        busy.redeem = true;
        api("POST", "/redeem", { code: c }).then((r) => {
          msg.success("已兑换 " + usd(r.value) + ",当前余额 " + usd(r.balance));
          code.value = "";
          s.reload();
        }).catch((e) => msg.error(e.message))
          .then(() => { busy.redeem = false; });
      };
      const lcols = [
        { title: "时间", key: "created_at", width: 150,
          render: (r) => h("span", { style: Object.assign({ fontSize: "12px" },
            MONO) }, absTime(r.created_at)) },
        { title: "来源", key: "reason", width: 116,
          render: (r) => tag.apply(null, reasonOf(r.reason)) },
        { title: "金额", key: "amount", align: "right", width: 112,
          render: (r) => money(r.amount) },
        { title: "变动后余额", key: "balance_after", align: "right", width: 124,
          render: (r) => h("span", MONO_ATTR, r.balance_after == null
            ? "—" : "$" + fmt(r.balance_after, 2)) },
        { title: "幂等键", key: "idem_key", minWidth: 190, render: (r) =>
            h(naive.NText, { depth: 3, style: SUBSTY }, () => r.idem_key || "—") },
      ];
      const OST = { pending: ["warning", "待支付"], paid: ["info", "已支付"],
        recharging: ["info", "到账中"], completed: ["success", "已完成"],
        expired: ["default", "已过期"], failed: ["error", "失败"] };
      const ocols = [
        { title: "订单号", key: "out_trade_no", minWidth: 190, render: (r) =>
            h("code", { style: Object.assign({ fontSize: "12px" }, MONO) },
              r.out_trade_no) },
        { title: "金额", key: "amount", align: "right", width: 96,
          render: (r) => h("span", MONO_ATTR, "$" + fmt(r.amount, 2)) },
        { title: "渠道", key: "provider", width: 132,
          render: (r) => r.provider === "baipiao" ? "白嫖社区毛支付" : r.provider },
        { title: "应付", key: "pay_amount_mao", width: 108,
          render: (r) => r.pay_amount_mao != null ? r.pay_amount_mao + " 毛"
            : r.pay_amount_cny != null ? "¥" + fmt(r.pay_amount_cny, 2) : "—" },
        { title: "状态", key: "status", width: 96, render: (r) => {
            const m = OST[r.status] || ["default", r.status];
            return tag(m[0], m[1]); } },
        { title: "创建时间", key: "created_at", width: 150, render: (r) =>
            h("span", { style: Object.assign({ fontSize: "12px" }, MONO) },
              absTime(r.created_at)) },
      ];
      return Object.assign({ bal, entries, orders, providers, inv, amount,
        provider, code, busy, lcols, ocols, pay, doRedeem, fmt, usd, go,
        minTopup, watching, maoRate, maoQuote }, s);
    },
    template: `
<Load :loading="loading" :error="error" :on-retry="reload">
<div class="console-page-stack">
<section class="console-hero">
  <div class="console-hero-copy">
    <div class="console-eyebrow">可用余额 · 美元</div>
    <div class="console-hero-title"><MetricValue :value="'$' + fmt(bal.balance, 2)"/></div>
    <p class="console-hero-note">充值、兑换与返佣计入同一账户，余额变动按实际计费结果记录。</p>
    <div class="console-hero-actions"><n-button secondary @click="go('#/billing')">查看套餐与单价</n-button></div>
  </div>
  <aside class="console-hero-aside">
    <div class="console-inline-stat"><span class="console-inline-stat-label">累计消费 · 含请求与套餐</span>
      <strong class="console-inline-stat-value"><MetricValue :value="'$' + fmt(bal.total_spent)"/></strong></div>
    <div class="console-inline-stat"><span class="console-inline-stat-label">累计返佣 · 已邀请 {{ inv.invitees || 0 }} 人</span>
      <strong class="console-inline-stat-value"><MetricValue :value="'$' + fmt(inv.earned, 2)"/></strong></div>
  </aside>
</section>
<div class="console-workspace-grid">
  <n-card class="console-panel" size="small" title="充值余额">
    <p class="console-section-note">选择金额与支付方式，支付完成后自动更新账户余额。单笔最低 {{ usd(minTopup) }}。</p>
    <n-space vertical :size="14">
      <n-radio-group v-model:value="amount" size="small">
        <n-radio-button :value="5">$5</n-radio-button>
        <n-radio-button :value="10">$10</n-radio-button>
        <n-radio-button :value="50">$50</n-radio-button>
        <n-radio-button :value="100">$100</n-radio-button>
      </n-radio-group>
      <n-input-number v-model:value="amount" :min="minTopup" :precision="2"
        style="width:100%"><template #prefix>$</template></n-input-number>
      <n-select v-model:value="provider" :options="providers"
        :disabled="!providers.length"
        :placeholder="providers.length ? '请选择支付方式' : '暂无可用支付方式'"/>
      <n-alert v-if="provider === 'baipiao'" type="info" :bordered="false" :show-icon="false">
        购买 {{ '$' + fmt(amount, 2) }} API 余额，预计需 {{ maoQuote }} 毛。
        每 $1 需 {{ maoRate }} 毛，不足一毛向上取整；下单后以该订单报价为准。
      </n-alert>
      <n-alert v-if="!providers.length" type="warning" :bordered="false"
        :show-icon="false" style="font-size:12.5px">
        在线充值暂未开放，请稍后再试或联系站长。</n-alert>
      <n-alert v-if="watching" type="info" :bordered="false"
        :show-icon="false" style="font-size:12.5px">
        <n-space align="center" :size="8">
          <n-spin :size="14"/>
          <span>等待 {{ watching.out_trade_no }} 到账,付款完成后这里会自动更新。</span>
        </n-space>
      </n-alert>
      <n-button type="primary" block :loading="busy.pay"
        :disabled="!providers.length" @click="pay">
        {{ !providers.length ? '暂未开放在线充值' : (provider === 'baipiao' ? '用毛购买 ' : '去支付 ') + '$' + fmt(amount, 2) }}</n-button>
    </n-space>
  </n-card>
  <n-card class="console-panel" size="small" title="兑换额度">
    <p class="console-section-note">已有兑换码，可在这里将额度计入当前账户。</p>
    <n-space vertical :size="14">
      <n-input v-model:value="code" placeholder="XXXX-XXXX-XXXX-XXXX"
        @keyup.enter="doRedeem"/>
      <n-alert type="default" :bordered="false" style="font-size:12.5px">
        兑换后额度立即计入余额，下方流水记录每笔变动。在线充值的流水来源显示为「充值到账」。</n-alert>
      <n-button block :loading="busy.redeem" @click="doRedeem">兑换</n-button>
    </n-space>
  </n-card>
</div>
<section class="console-table-card">
  <div class="console-section-heading">
    <div><h2 class="console-section-title">账户记录</h2>
      <p class="console-section-note">核对余额变动，或查看在线充值订单的支付与到账状态。</p></div>
    <n-button secondary :loading="loading" @click="reload">刷新记录</n-button>
  </div>
  <n-tabs class="console-tabs" type="line" default-value="ledger" animated>
    <n-tab-pane name="ledger" tab="余额流水">
      <n-data-table :columns="lcols" :data="entries" :bordered="false" size="small"
        :scroll-x="792" :row-key="(r) => r.id"
        :pagination="{pageSize:10,pageSlot:5,prefix:({itemCount})=>'总计 '+itemCount}"/>
    </n-tab-pane>
    <n-tab-pane name="orders" tab="充值订单">
      <n-data-table :columns="ocols" :data="orders" :bordered="false" size="small"
        :scroll-x="772" :row-key="(r) => r.id"
        :pagination="{pageSize:10,pageSlot:5,prefix:({itemCount})=>'总计 '+itemCount}"/>
    </n-tab-pane>
  </n-tabs>
</section>
</div>
</Load>`,
  };
  const Leaderboard = {
    components: { Load, Ic },
    setup() {
      const result = ref({ rows: [], me: null });
      const range = ref("all"), page = ref(1), pageSize = 20;
      const loading = ref(true), error = ref("");
      let requestVersion = 0;
      const ranges = [{ label: "累计", value: "all" },
        { label: "近 7 天", value: "7d" }, { label: "近 30 天", value: "30d" }];
      const reload = async () => {
        const version = ++requestVersion;
        loading.value = true; error.value = "";
        try {
          const data = await api("GET", "/leaderboard" + qs({
            range: range.value, page: page.value, page_size: pageSize }));
          if (version === requestVersion) result.value = data;
        } catch (e) {
          if (version === requestVersion) error.value = e.message || String(e);
        } finally {
          if (version === requestVersion) loading.value = false;
        }
      };
      const setRange = (value) => { range.value = value; page.value = 1; };
      watch([range, page], reload);
      onMounted(reload);
      onUnmounted(() => { requestVersion += 1; });
      const cols = [
        { title: "排名 / 用户", key: "name", minWidth: 130, render: (r) =>
          h("div", { class: "console-rank-identity" }, [
            h("span", { class: ["console-rank-number", r.rank && r.rank <= 3 ? "is-leading" : ""] }, r.rank || "—"),
            h("div", { class: "console-rank-user" }, [
              h("div", {}, [h("strong", {}, r.name), r.is_me ? h("span", { class: "console-rank-me" }, "我") : null]),
              h("span", { class: "console-section-note" }, nf(r.requests) + " 次请求"),
            ]),
          ]) },
        { title: "Token 消耗", key: "total_tokens", align: "right", minWidth: 150,
          render: (r) => h("div", { class: "console-rank-tokens" }, [
            h("strong", {}, nf(r.total_tokens)),
            h("span", {}, "输入 " + nf(r.input_tokens)),
            h("span", {}, "输出 " + nf(r.output_tokens)),
            h("span", {}, r.requests && !r.cache_rows ? "缓存未提供" : "缓存 " + nf(r.cache_tokens)
              + (r.cache_rows < r.requests ? "（部分记录）" : "")),
          ]) },
      ];
      const pagination = computed(() => ({ page: page.value, pageSize,
        itemCount: result.value.total_users || 0, pageSlot: 5,
        onChange: (value) => { page.value = value; },
        prefix: ({ itemCount }) => "共 " + nf(itemCount) + " 位用户" }));
      const rangeLabel = computed(() => (ranges.find((r) => r.value === result.value.range) || ranges[0]).label);
      return { result, range, ranges, page, loading, error, reload, setRange,
        cols, pagination, rangeLabel, nf, absTime, go,
        rowClass: (r) => r.is_me ? "console-rank-current" : "" };
    },
    template: `
<div class="console-page-stack">
  <div class="console-toolbar console-rank-toolbar">
    <n-radio-group :value="range" @update:value="setRange" aria-label="排行榜统计范围">
      <n-radio-button v-for="r in ranges" :key="r.value" :value="r.value">{{ r.label }}</n-radio-button>
    </n-radio-group>
    <n-button secondary :loading="loading" @click="reload">刷新榜单</n-button>
  </div>
  <Load :loading="loading" :error="error" :on-retry="reload">
    <div class="console-page-stack">
      <section class="console-hero">
        <div class="console-hero-copy">
          <div class="console-eyebrow">{{ rangeLabel }} · 我的 Token 消耗</div>
          <div class="console-hero-title console-rank-total">{{ result.me ? nf(result.me.total_tokens) : '—' }}<span>Token</span></div>
          <p class="console-hero-note">{{ result.me && result.me.rank ? '当前排名第 ' + nf(result.me.rank) + ' 名' : '这个范围内还没有 Token 用量' }}<template v-if="result.total_users != null"> · 共 {{ nf(result.total_users) }} 位启用用户</template></p>
          <div class="console-hero-actions"><n-button secondary @click="go('#/usage')">查看我的使用记录</n-button></div>
        </div>
        <aside class="console-hero-aside">
          <div class="console-section-title">每一次使用，都有记录</div>
          <p class="console-section-note">按输入、输出和缓存 Token 的合计降序排列，与概览用量口径一致。相同用量并列，无用量的用户显示在榜单末尾。</p>
          <p class="console-section-note">缓存只计入可计算的记录；「缓存未提供」或「部分记录」表示该用户的缓存统计不完整。</p>
          <p class="console-section-note">用户名已脱敏，仅登录后可查看。{{ result.as_of ? '更新于 ' + absTime(result.as_of) : '' }}</p>
        </aside>
      </section>
      <section class="console-table-card console-rank-table">
        <div class="console-section-heading"><div><h2 class="console-section-title">Token 消耗排行</h2>
          <p class="console-section-note">{{ rangeLabel }}合计 {{ result.total_tokens != null ? nf(result.total_tokens) : '—' }} Token · 已禁用的账号不参与展示</p></div></div>
        <n-data-table :columns="cols" :data="result.rows" :row-key="r => r.position"
          :row-class-name="rowClass" :bordered="false" :pagination="pagination" remote size="small">
          <template #empty><n-empty description="暂时没有可展示的用户"/></template>
        </n-data-table>
      </section>
    </div>
  </Load>
</div>`,
  };

  const Invite = {
    components: { Load, StatCard },
    setup() {
      const inv = ref({});
      const affRows = ref([]);
      const s = useFetch(() => Promise.all([api("GET", "/invite"),
        api("GET", "/ledger?limit=200&reason=affiliate")]).then((r) => {
          inv.value = r[0];
          affRows.value = r[1].entries || [];
        }));
      const link = computed(() => location.origin +
        (P.BASE || "") + "/portal#/register?ref=" + (inv.value.aff_code || ""));
      const cols = [
        { title: "时间", key: "created_at", width: 150, render: (r) =>
            h("span", { style: Object.assign({ fontSize: "12px" }, MONO) },
              absTime(r.created_at)) },
        { title: "返佣", key: "amount", align: "right", width: 110,
          render: (r) => money(r.amount) },
        { title: "触发", key: "idem_key", minWidth: 210, render: (r) =>
            h(naive.NText, { depth: 3, style: SUBSTY }, () =>
              r.meta && r.meta.kind === "fixed_invite" ? "有效注册奖励" : r.idem_key || "—") },
      ];
      return Object.assign({ inv, affRows, cols, link, fmt, go,
        copyCode: () => copy(inv.value.aff_code || "", "已复制返佣码"),
        copyLink: () => copy(link.value, "已复制邀请链接") }, s);
    },
    template: `
<Load :loading="loading" :error="error" :on-retry="reload">
<div class="console-page-stack">
<section class="console-hero">
  <div class="console-hero-copy">
    <div class="console-eyebrow">邀请带来的累计返佣</div>
    <div class="console-hero-title"><MetricValue :value="'$' + fmt(inv.earned, 4)"/></div>
    <p class="console-hero-note">{{ inv.invitees || 0 }} 位用户已绑定邀请关系，返佣按实际入账累计。</p>
    <div class="console-hero-actions"><n-button type="primary" @click="copyLink">复制邀请链接</n-button>
      <n-button secondary @click="go('#/wallet')">查看钱包</n-button></div>
  </div>
  <aside class="console-hero-aside">
    <div class="console-section-title">分享后，查看实际入账</div>
    <p class="console-section-note">返佣金额以每笔到账记录为准。邀请关系绑定后持续有效。</p>
  </aside>
</section>
<div class="console-workspace-grid">
  <n-card class="console-panel" size="small" title="分享邀请">
    <n-space vertical :size="14">
      <div class="console-eyebrow">我的返佣码</div>
      <n-input-group>
        <n-input :value="inv.aff_code || ''" readonly/>
        <n-button type="primary" ghost @click="copyCode">复制</n-button>
      </n-input-group>
      <div class="console-eyebrow">专属邀请链接</div>
      <n-input-group>
        <n-input :value="link" readonly/>
        <n-button ghost @click="copyLink">复制链接</n-button>
      </n-input-group>
    </n-space>
  </n-card>
  <n-card class="console-panel" size="small" title="邀请规则">
    <template v-if="inv.reward_policy && inv.reward_policy.enabled">
      <div class="console-inline-stat"><span class="console-inline-stat-label">每位新用户有效注册后</span><strong class="console-inline-stat-value">\${{ fmt(inv.reward_policy.amount, 4) }}</strong></div>
      <p class="console-section-note">奖励自动记入邀请人的钱包。已获得 {{ inv.rewarded_invitees }} 人的注册奖励；{{ inv.reward_policy.max_invitees ? '每位邀请人最多奖励 ' + inv.reward_policy.max_invitees + ' 人' : '奖励人数不限' }}。</p>
      <p v-if="inv.reward_policy.max_invitees && inv.rewarded_invitees >= inv.reward_policy.max_invitees" class="console-section-note">已达到奖励人数上限，后续邀请仍会绑定关系，本项奖励不再发放。</p>
    </template>
    <p v-else class="console-section-note">当前未开启注册邀请奖励。仍可分享链接记录新用户邀请关系，已有入账保留。</p>
    <p v-if="inv.registration_policy && inv.registration_policy.community_only" class="console-section-note">新用户须通过白嫖社区授权、达到 Lv{{ inv.registration_policy.community_min_level }} 及以上并核验邮箱；邀请关系不改变注册门槛。</p>
    <p v-else class="console-section-note">邮箱注册须完成邮件验证；邀请制开启时仍须使用管理员的一次性注册邀请码，个人返佣码不能代替它。</p>
    <p class="console-section-note">仅新账号首次注册绑定邀请人，老账号登录或绑定社区不会新增邀请，也不会重复发奖。奖励按注册时生效的规则结算；新设置不补发历史邀请。</p>
  </n-card>
</div>
<n-card class="console-table-card" size="small" title="返佣入账">
  <n-data-table :columns="cols" :data="affRows" :bordered="false" size="small"
    :scroll-x="470" :row-key="(r) => r.id"
    :pagination="{pageSize:10,pageSlot:5,prefix:({itemCount})=>'总计 '+itemCount}"/>
</n-card>
</div>
</Load>`,
  };
  /* 共享 Key:把自己的上游 key 交上来换余额奖励。
     规则与「我交过哪些」都来自 /api/share/keys;提交走 POST /api/share/keys,
     服务端会先拿这把 key 真发一次请求验活,通过才入库并按把发奖。 */
  const Share = {
    components: { Load, StatCard },
    setup() {
      const info = ref({});
      const rows = ref([]);
      const form = reactive({ channel: "", key: "" });
      const busy = ref(false);
      const s = useFetch(() => api("GET", "/share/keys").then((r) => {
        info.value = r;
        rows.value = r.items || [];
        if (!form.channel && (r.channels || []).length) form.channel = r.channels[0];
      }));
      const options = computed(() =>
        (info.value.channels || []).map((c) => ({ label: c, value: c })));
      const submit = () => {
        if (!info.value.enabled) return msg.warning("站点当前未开启共享");
        if (!form.channel) return msg.warning("先选一个渠道");
        const key = String(form.key || "").trim();
        if (key.length < 8) return msg.warning("粘贴完整的 key");
        busy.value = true;
        api("POST", "/share/keys", { channel: form.channel, key })
          .then((r) => {
            msg.success("已收录，奖励 $" + Number(r.reward).toFixed(4) + " 已入账");
            form.key = "";
            return s.reload();
          })
          .catch((e) => msg.error(e.message))
          .then(() => { busy.value = false; });
      };
      const cols = [
        { title: "渠道", key: "channel", width: 120 },
        { title: "标识", key: "identity", minWidth: 190, render: (r) =>
            h("span", { style: Object.assign({ fontSize: "12px" }, MONO) },
              r.identity || "—") },
        { title: "验活模型", key: "probe_model", minWidth: 150, render: (r) =>
            h("span", { style: Object.assign({ fontSize: "12px" }, MONO) },
              r.probe_model || "—") },
        { title: "状态", key: "status", width: 100 },
        { title: "时间", key: "shared_at", width: 150, render: (r) =>
            h("span", { style: Object.assign({ fontSize: "12px" }, MONO) },
              absTime(r.shared_at)) },
      ];
      return Object.assign({ info, rows, form, busy, options, submit, cols, fmt, go }, s);
    },
    template: `
<Load :loading="loading" :error="error" :on-retry="reload">
<div class="console-page-stack">
<section class="console-hero">
  <div class="console-hero-copy">
    <div class="console-eyebrow">共享带来的累计奖励</div>
    <div class="console-hero-title"><MetricValue :value="'$' + fmt(info.earned, 4)"/></div>
    <p class="console-hero-note">已收录 {{ info.rewarded_count || 0 }} 把 key，奖励按每把入账累计。</p>
    <div class="console-hero-actions"><n-button secondary @click="go('#/wallet')">查看钱包</n-button>
      <n-button secondary @click="go('#/keys')">我的 API 密钥</n-button></div>
  </div>
  <aside class="console-hero-aside">
    <div class="console-section-title">先验活，再计奖</div>
    <p class="console-section-note">提交后系统会拿这把 key 真发一次请求。上游拒收的不入库、不发奖。</p>
  </aside>
</section>
<div class="console-workspace-grid">
  <n-card class="console-panel" size="small" title="提交一把 key">
    <n-space vertical :size="14">
      <n-form-item label="渠道" :show-feedback="false">
        <n-select v-model:value="form.channel" :options="options"
          :disabled="!info.enabled || !options.length"
          placeholder="该站点暂无可共享的渠道" aria-label="选择渠道"/>
      </n-form-item>
      <n-form-item label="API Key" :show-feedback="false">
        <n-input v-model:value="form.key" type="password" show-password-on="click"
          placeholder="粘贴上游的 key" aria-label="要共享的 key"/>
      </n-form-item>
      <n-button type="primary" :loading="busy" :disabled="!info.enabled" @click="submit">提交并验活</n-button>
    </n-space>
  </n-card>
  <n-card class="console-panel" size="small" title="规则">
    <template v-if="info.enabled">
      <div class="console-inline-stat"><span class="console-inline-stat-label">每把有效 key</span><strong class="console-inline-stat-value">\${{ fmt(info.amount, 4) }}</strong></div>
      <p class="console-section-note">可共享的渠道：{{ (info.channels || []).join('、') || '全部' }}。</p>
      <p class="console-section-note">{{ info.max_per_user ? '每位用户最多拿 ' + info.max_per_user + ' 把的奖励；' : '每人可拿奖励的把数不限；' }}同一把 key 全站只发一次奖。</p>
    </template>
    <p v-else class="console-section-note">站点当前未开启共享 Key。</p>
    <p class="console-section-note">你交的 key 会被当作本站上游号池使用。只交你愿意共享的 key，并确认它允许这样使用。</p>
  </n-card>
</div>
<n-card class="console-table-card" size="small" title="我提交的 key">
  <n-data-table :columns="cols" :data="rows" :bordered="false" size="small"
    :scroll-x="760" :row-key="(r) => r.channel + r.identity"
    :pagination="{pageSize:10,pageSlot:5,prefix:({itemCount})=>'总计 '+itemCount}"/>
</n-card>
</div>
</Load>`,
  };
  const Profile = {
    components: { Load, SectionHead, Avatar, Ic },
    setup() {
      const me = ref({});
      const pw = reactive({ old: "", now: "", again: "" });
      const bind = reactive({ email: "", code: "" });
      const busy = reactive({ pw: false, send: false, verify: false,
        save: false, community: false, totp: false });
      /* TOTP:secret 非空 = 正在绑定(等验证码确认);已开启时 password + code 用于关闭 */
      const tf = reactive({ secret: "", uri: "", code: "", password: "" });
      const totpSetup = () => {
        busy.totp = true;
        api("POST", "/2fa/setup")
          .then((r) => { tf.secret = r.secret; tf.uri = r.otpauth_uri; tf.code = ""; })
          .catch((e) => msg.error(e.message)).then(() => { busy.totp = false; });
      };
      const totpEnable = () => {
        if ((tf.code || "").trim().length < 6) return msg.warning("填 authenticator 里当前的 6 位码");
        busy.totp = true;
        api("POST", "/2fa/enable", { code: tf.code.trim() })
          .then(() => {
            msg.success("二次验证已开启,下次登录要带验证码");
            tf.secret = tf.uri = tf.code = "";
            return s.reload();
          })
          .catch((e) => msg.error(e.message)).then(() => { busy.totp = false; });
      };
      const totpDisable = () => {
        if (!tf.password) return msg.warning("填当前密码");
        if ((tf.code || "").trim().length < 6) return msg.warning("填当前的 6 位验证码");
        busy.totp = true;
        api("POST", "/2fa/disable", { password: tf.password, code: tf.code.trim() })
          .then(() => {
            msg.success("二次验证已关闭");
            tf.password = tf.code = "";
            return s.reload();
          })
          .catch((e) => msg.error(e.message)).then(() => { busy.totp = false; });
      };
      /* 只打 /me:分组与计费策略搬到「计费与套餐」页与顶栏下拉,
         concurrency_limit 已随 /me 一起返回,不必再要一次 /billing。 */
      const s = useFetch(() => api("GET", "/me").then((r) => {
        me.value = r;
        form.name = r.display_name || "";
        form.avatar = r.avatar || "";
      }));
      /* 头像与昵称先改本地副本,点保存才提交:参考图里「上传图片 / 保存 /
         删除」是三个动作,选完图能先看预览再决定要不要落库。 */
      const form = reactive({ name: "", avatar: "" });
      const dirty = computed(() =>
        form.name !== (me.value.display_name || "") ||
        form.avatar !== (me.value.avatar || ""));

      /* 昵称为空就回落邮箱前缀,不显示空白 —— 顶栏也是这个规则。 */
      const shown = computed(() => me.value.display_name ||
        String(me.value.email || "").split("@")[0] || "未登录");
      const initial = computed(() => shown.value.slice(0, 1).toUpperCase());
      const regMonth = computed(() => {
        const t = me.value.created_at;
        if (!t) return "—";
        const d = new Date(t * 1000);
        return d.getFullYear() + " 年 " + (d.getMonth() + 1) + " 月";
      });

      function pickFile() {
        const el = document.createElement("input");
        el.type = "file";
        el.accept = "image/png,image/jpeg,image/webp,image/gif";
        el.onchange = () => {
          const f = el.files && el.files[0];
          if (!f) return;
          shrinkAvatar(f).then((uri) => {
            form.avatar = uri;
            msg.success("已选择,记得点保存");
          }).catch((e) => msg.error(e.message));
        };
        el.click();
      }
      const saveProfile = () => {
        busy.save = true;
        api("PATCH", "/me", { display_name: form.name, avatar: form.avatar })
          .then(() => {
            msg.success("资料已更新");
            window.dispatchEvent(new Event("bitapi:me-changed"));
            return s.reload();
          })
          .catch((e) => msg.error(e.message))
          .then(() => { busy.save = false; });
      };
      const dropAvatar = () => {
        if (!me.value.avatar && !form.avatar) return msg.info("当前没有头像");
        dlg.warning({ title: "删除头像", positiveText: "删除",
          negativeText: "取消", content: "删除后回到邮箱首字母头像。",
          onPositiveClick: () => {
            form.avatar = "";
            return api("PATCH", "/me", { avatar: "" })
              .then(() => {
                msg.success("已删除头像");
                window.dispatchEvent(new Event("bitapi:me-changed"));
                return s.reload();
              })
              .catch((e) => msg.error(e.message));
          } });
      };

      const savePw = () => {
        if (pw.now.length < 6) return msg.warning("新密码至少 6 位");
        if (pw.now !== pw.again) return msg.warning("两次输入的新密码不一致");
        busy.pw = true;
        api("POST", "/me/password", { old_password: pw.old, new_password: pw.now })
          .then((r) => {
            /* 改密让旧会话作废(含本机这个),服务端顺手回了新会话,换上就不掉线 */
            if (r.token) setToken(r.token);
            msg.success("密码已更新,其他设备上的登录已失效");
            pw.old = pw.now = pw.again = "";
          })
          .catch((e) => msg.error(e.message)).then(() => { busy.pw = false; });
      };
      const send = () => {
        const e = bind.email.trim();
        if (!e) return msg.warning("请填写邮箱");
        busy.send = true;
        api("POST", "/email/bind", { email: e })
          .then((r) => (r.sent
            ? msg.success("验证码已发到 " + e + ",15 分钟内有效")
            : msg.warning(r.message || "站点未配置邮件服务,请联系站长")))
          .catch((x) => msg.error(x.message)).then(() => { busy.send = false; });
      };
      const verify = () => {
        if (!bind.code.trim()) return msg.warning("请输入验证码");
        busy.verify = true;
        api("POST", "/email/verify", { code: bind.code.trim() })
          .then(() => { msg.success("邮箱已验证"); bind.code = ""; s.reload(); })
          .catch((x) => msg.error(x.message)).then(() => { busy.verify = false; });
      };
      const bindCommunity = () => {
        busy.community = true;
        api("POST", "/community/auth/start", { purpose: "bind" })
          .then((r) => {
            if (!r.authorize_url) throw new Error("社区绑定地址未配置");
            sessionStorage.setItem("bitapi_community_purpose", "bind");
            location.assign(r.authorize_url);
          })
          .catch((x) => msg.error(x.message))
          .then(() => { busy.community = false; });
      };
      return Object.assign({ me, pw, bind, busy, savePw, send,
        verify, bindCommunity, absTime, relTime, go, fmt, MONO,
        form, dirty, shown, initial, regMonth, pickFile, saveProfile,
        dropAvatar, copy, tf, totpSetup, totpEnable, totpDisable,
        focusSection: (id) => {
          const section = document.getElementById(id);
          if (section) { section.scrollIntoView({ block: "start" }); section.focus({ preventScroll: true }); }
        } }, s);
    },
    template: `
<Load :loading="loading" :error="error" :on-retry="reload">
<div class="console-page-stack">
<section class="console-hero">
  <div class="console-hero-copy">
    <div class="console-eyebrow">个人账户</div>
    <div class="console-hero-title">{{ shown }}</div>
    <p class="console-hero-note">{{ me.email }} · {{ regMonth }} 加入</p>
      <n-space align="center" :size="8" style="margin-bottom:3px">
        <n-tag size="small" round :bordered="false"
          :type="me.role === 'admin' ? 'warning' : 'default'">
          {{ me.role === 'admin' ? '管理员' : '用户' }}</n-tag>
        <n-tag size="small" round :bordered="false"
          :type="me.status === 'active' ? 'success' : 'error'">
          {{ me.status === 'active' ? '启用' : '已禁用' }}</n-tag>
      </n-space>
  </div>
  <aside class="console-hero-aside"><Avatar :src="me.avatar" :text="initial" :size="80" radius="24px"/></aside>
</section>
<div class="console-resource-strip">
  <div class="console-inline-stat"><span class="console-inline-stat-label">账户余额</span>
    <strong class="console-inline-stat-value"><MetricValue :value="'$' + fmt(me.balance)"/></strong></div>
  <div class="console-inline-stat"><span class="console-inline-stat-label">累计消费</span>
    <strong class="console-inline-stat-value"><MetricValue :value="'$' + fmt(me.total_spent)"/></strong></div>
  <div class="console-inline-stat"><span class="console-inline-stat-label">并发上限</span>
    <strong class="console-inline-stat-value">{{ me.concurrency_limit ? me.concurrency_limit + ' 个请求' : '不限' }}</strong></div>
  <div class="console-inline-stat"><span class="console-inline-stat-label">邮箱验证</span>
    <strong class="console-inline-stat-value">{{ me.email_verified ? '已验证' : '未验证' }}</strong></div>
</div>
<div class="console-profile-layout">
  <nav class="console-anchor-nav" aria-label="资料分区">
    <button type="button" @click="focusSection('profile-identity')">资料与头像</button>
    <button type="button" @click="focusSection('profile-login')">登录方式</button>
    <button v-if="me.has_password" type="button" @click="focusSection('profile-password')">登录密码</button>
    <button v-if="me.has_password" type="button" @click="focusSection('profile-security')">二次验证</button>
  </nav>
  <div class="console-page-stack">
<n-card id="profile-identity" class="console-anchor-section" tabindex="-1" size="small">
  <SectionHead icon="user" title="资料与头像"
    sub="维护公开展示信息,并保持头像与昵称风格一致" color="#4a6fa5"/>
  <n-grid :cols="'1 900:2'" :x-gap="14" :y-gap="14" responsive="self"
    style="margin-top:16px">
    <n-gi><div class="subpanel">
      <Avatar :src="form.avatar" :text="initial" :size="64" radius="16px"/>
      <div style="margin-top:12px;font-size:13px;font-weight:650">资料头像</div>
      <n-text depth="3" style="font-size:11.5px;display:block;margin-top:4px">
        上传时自动裁成正方形并压缩到 20KB 以内;GIF 不压缩,需自行控制在
        20KB 以内。
      </n-text>
      <n-space :size="8" style="margin-top:12px">
        <n-button size="small" secondary @click="pickFile">
          <template #icon><Ic name="image" :size="14"/></template>
          上传图片</n-button>
        <n-button size="small" type="primary" :disabled="!dirty"
          :loading="busy.save" @click="saveProfile">保存</n-button>
        <n-button size="small" secondary type="error"
          :disabled="!me.avatar && !form.avatar" @click="dropAvatar">
          删除</n-button>
      </n-space>
    </div></n-gi>
    <n-gi><div class="subpanel">
      <div style="font-size:13px;font-weight:650;margin-bottom:12px">
        编辑个人资料</div>
      <n-form label-placement="top">
        <n-form-item label="昵称" :show-feedback="false"
          style="margin-bottom:6px">
          <n-input v-model:value="form.name" placeholder="留空则显示邮箱前缀"
            clearable maxlength="32" show-count/>
        </n-form-item>
      </n-form>
      <n-text depth="3" style="font-size:11.5px;display:block;margin-top:8px">
        昵称只用于界面展示,登录仍用邮箱。
      </n-text>
      <n-space justify="end" style="margin-top:14px">
        <n-button size="small" type="primary" :disabled="!dirty"
          :loading="busy.save" @click="saveProfile">更新资料</n-button>
      </n-space>
    </div></n-gi>
  </n-grid>
</n-card>

<n-card id="profile-login" class="console-anchor-section" tabindex="-1" size="small">
  <SectionHead icon="mail" title="登录方式绑定"
    sub="查看当前绑定状态,并把更多登录方式关联到这个账号" color="#15803d"/>
  <div class="bindrow" style="margin-top:16px">
    <span class="bindicon" style="background:rgba(127,127,127,.12)">
      <Ic name="mail" :size="17" color="#4a6fa5"/></span>
    <div style="flex:1;min-width:0">
      <n-space align="center" :size="7">
        <span style="font-size:13px;font-weight:600">邮箱</span>
        <n-tag size="small" round :bordered="false"
          :type="me.email_verified ? 'success' : 'warning'">
          {{ me.email_verified ? '已验证' : '未验证' }}</n-tag>
      </n-space>
      <div class="bindsub" :style="MONO">{{ me.email }}</div>
      <div class="bindsub" v-if="me.email_verified">
        验证于 {{ absTime(me.email_verified_at) }}
      </div>
      <div class="bindsub" v-else>
        填邮箱、点发送,把收到的 6 位验证码填回来。找回密码也走这个邮箱。
      </div>
    </div>
    <n-space vertical :size="6" style="width:220px">
      <n-input-group>
        <n-input v-model:value="bind.email" size="small"
          placeholder="you@example.com"/>
        <n-button size="small" ghost :loading="busy.send" @click="send">
          发码</n-button>
      </n-input-group>
      <n-input-group>
        <n-input v-model:value="bind.code" size="small" placeholder="验证码"
          @keyup.enter="verify"/>
        <n-button size="small" type="primary" ghost :loading="busy.verify"
          @click="verify">验证</n-button>
      </n-input-group>
    </n-space>
  </div>
  <div class="bindrow" style="margin-top:10px">
    <span class="bindicon" style="background:rgba(127,127,127,.12)">
      <Ic name="user" :size="17" color="#a8613c"/></span>
    <div style="flex:1;min-width:0">
      <n-space align="center" :size="7">
        <span style="font-size:13px;font-weight:600">白嫖社区</span>
        <n-tag size="small" round :bordered="false"
          :type="me.community ? 'success' : 'default'">
          {{ me.community ? '已绑定' : '未绑定' }}</n-tag>
      </n-space>
      <template v-if="me.community">
        <div class="bindsub">
          {{ me.community.name || me.community.username || '社区用户' }}
          <span v-if="me.community.username"> (@{{ me.community.username }})</span>
        </div>
        <div class="bindsub">
          绑定于 {{ absTime(me.community.bound_at || me.community.created_at) }}
        </div>
      </template>
      <div v-else class="bindsub">
        绑定后可直接使用社区账号登录 bit-api。
      </div>
    </div>
    <n-button v-if="!me.community" size="small" type="primary" ghost
      :loading="busy.community" @click="bindCommunity">绑定社区账号</n-button>
  </div>
</n-card>

<n-card v-if="me.has_password" id="profile-password" class="console-anchor-section" tabindex="-1" size="small">
  <SectionHead icon="shield" title="安全" sub="修改登录密码" color="#b91c1c"/>
  <n-form label-placement="top" style="margin-top:16px">
    <n-grid :cols="'1 760:3'" :x-gap="12" :y-gap="12" responsive="self">
      <n-gi><n-form-item label="当前密码" :show-feedback="false">
        <n-input v-model:value="pw.old" type="password" show-password-on="click"
          placeholder="••••••••"
          :input-props="{autocomplete:'current-password'}"/></n-form-item></n-gi>
      <n-gi><n-form-item label="新密码" :show-feedback="false">
        <n-input v-model:value="pw.now" type="password" show-password-on="click"
          placeholder="至少 6 位"
          :input-props="{autocomplete:'new-password'}"/></n-form-item></n-gi>
      <n-gi><n-form-item label="确认新密码" :show-feedback="false">
        <n-input v-model:value="pw.again" type="password"
          show-password-on="click" placeholder="再输入一次"
          :input-props="{autocomplete:'new-password'}"/></n-form-item></n-gi>
    </n-grid>
  </n-form>
  <n-space justify="end" style="margin-top:14px">
    <n-button type="primary" size="small" :loading="busy.pw" @click="savePw">
      更新密码</n-button>
  </n-space>
</n-card>
<n-alert v-else type="default" :bordered="false" :show-icon="false">
  当前账号仅使用白嫖社区登录，无需填写本站密码。
</n-alert>

<n-card v-if="me.has_password" id="profile-security" class="console-anchor-section" tabindex="-1" size="small">
  <SectionHead icon="key" title="二次验证(TOTP)"
    :sub="me.totp_enabled ? '已开启:登录要密码 + authenticator 里的 6 位码' : '未开启。管理员账号建议开启 —— 管理台管的是钱'"
    :color="me.totp_enabled ? '#15803d' : '#a17a10'"/>
  <div style="margin-top:14px">
    <template v-if="!me.totp_enabled && !tf.secret">
      <n-button size="small" type="primary" ghost :loading="busy.totp" @click="totpSetup">
        开启二次验证</n-button>
    </template>
    <template v-else-if="!me.totp_enabled">
      <n-space vertical :size="10">
        <n-text depth="3" style="font-size:12.5px">
          在 Google Authenticator / 1Password / Aegis 里「手动输入密钥」,或把下面的
          otpauth 链接导入;然后填一个当前显示的 6 位码确认。密钥只显示这一次。</n-text>
        <n-input :value="tf.secret" readonly :style="MONO">
          <template #suffix><n-button text size="tiny" @click="copy(tf.secret, '已复制密钥')">复制</n-button></template>
        </n-input>
        <n-input :value="tf.uri" readonly type="textarea" :autosize="{minRows:1,maxRows:3}"
          style="font-size:11.5px">
        </n-input>
        <n-input-group style="width:280px">
          <n-input v-model:value="tf.code" placeholder="6 位验证码" maxlength="8"
            :input-props="{inputmode:'numeric'}" @keyup.enter="totpEnable"/>
          <n-button type="primary" :loading="busy.totp" @click="totpEnable">确认开启</n-button>
        </n-input-group>
        <n-button text size="tiny" @click="tf.secret = ''">取消</n-button>
      </n-space>
    </template>
    <template v-else>
      <n-space align="end" :size="10" wrap>
        <n-form-item label="当前密码" :show-feedback="false" label-placement="top">
          <n-input v-model:value="tf.password" type="password" show-password-on="click"
            placeholder="••••••••" style="width:180px"/></n-form-item>
        <n-form-item label="验证码" :show-feedback="false" label-placement="top">
          <n-input v-model:value="tf.code" placeholder="123456" maxlength="8" style="width:140px"
            :input-props="{inputmode:'numeric'}"/></n-form-item>
        <n-button size="small" type="error" secondary :loading="busy.totp" @click="totpDisable">
          关闭二次验证</n-button>
      </n-space>
      <n-text depth="3" style="font-size:11.5px;display:block;margin-top:8px">
        手机丢了:请另一位管理员在「用户」页替你关闭。
      </n-text>
    </template>
  </div>
</n-card>
</div>
</div>
</div>
</Load>`,
  };
  /* 定价表:同一行既要能看普通档,又要能看长文本档,所以按「普通 → 长档」上下两行排。 */
  const priceCols = () => [
    { title: "模型", key: "model_pattern", minWidth: 210, fixed: "left",
      render: (r) => modelChip(r.model_pattern) },
    { title: "计费模式", key: "billing_mode", width: 96,
      render: (r) => tag(r.billing_mode === "free" ? "success"
        : r.billing_mode === "per_request" ? "warning" : "default",
        MODE[r.billing_mode] || r.billing_mode) },
    { title: "输入", key: "input_price", width: 118, align: "right",
      render: (r) => (r.billing_mode === "per_request"
        ? h("span", MONO_ATTR, "$" + fmt(r.per_request_price, 4) + " /次")
        : stack(h("span", { style: Object.assign({ color: C.in, fontWeight: 600 },
            MONO) }, pxCell(r.input_price)),
          r.long_threshold && r.long_input_price != null
            ? pxCell(r.long_input_price) : null, "flex-end")) },
    { title: "输出", key: "output_price", width: 118, align: "right",
      render: (r) => (r.billing_mode === "per_request" ? dash(null)
        : stack(h("span", { style: Object.assign({ color: C.out, fontWeight: 600 },
            MONO) }, pxCell(r.output_price)),
          r.long_threshold && r.long_output_price != null
            ? pxCell(r.long_output_price) : null, "flex-end")) },
    { title: "缓存读", key: "cache_read_price", width: 118, align: "right",
      render: (r) => (r.billing_mode === "per_request" ? dash(null)
        : stack(h("span", { style: Object.assign({ color: C.cr }, MONO) },
            pxCell(r.cache_read_price)),
          r.long_threshold && r.long_cache_read_price != null
            ? pxCell(r.long_cache_read_price) : null, "flex-end")) },
    { title: "缓存写", key: "cache_write_price", width: 118, align: "right",
      render: (r) => (r.billing_mode === "per_request" ? dash(null)
        : stack(h("span", { style: Object.assign({ color: C.cw }, MONO) },
            pxCell(r.cache_write_price)),
          r.long_threshold && r.long_cache_write_price != null
            ? pxCell(r.long_cache_write_price) : null, "flex-end")) },
    { title: "长文本阈值", key: "long_threshold", width: 132, align: "right",
      render: (r) => (r.long_threshold
        ? wrapTip(h("span", { style: Object.assign({ color: C.ctx,
            fontWeight: 600, cursor: "help" }, MONO) },
            "> " + nf(r.long_threshold)), [
            tipRow("判定量", "输入 + 缓存写 + 缓存读", CT.ctx),
            tipRow("不含", "输出 token", CT.out),
            tipRow("跳档后", "整次全部换长档单价", CT.cw, true)])
        : h("span", { style: { opacity: 0.35 } }, "不启用")) },
  ];

  const Billing = {
    components: { Load, StatCard },
    setup() {
      const b = ref({});
      const p = ref({ plans: [], current: null });
      const buying = ref(0);
      /* 剩余时长要走字:一张两小时的卡,页面开着不动就一直显示买下那一刻的数。
         30 秒一跳足够,离开这一页就收掉计时器。 */
      const tick = ref(0);
      let timer = setInterval(() => { tick.value += 1; }, 30000);
      onUnmounted(() => { if (timer) { clearInterval(timer); timer = null; } });

      const s = useFetch(() => Promise.all([
        api("GET", "/billing"), api("GET", "/plans"),
      ]).then((r) => { b.value = r[0]; p.value = r[1]; }));

      const policy = computed(() => b.value.policy || "free");
      const plans = computed(() => p.value.plans || []);
      const cur = computed(() => p.value.current || null);
      const bal = computed(() => p.value.balance || 0);
      /* 到期后 current 仍在(要能告诉用户「已回落」),active 才代表生效中。 */
      const onPlan = computed(() => !!(cur.value && cur.value.active));
      const left = computed(() => (cur.value
        ? (tick.value, planLeft(cur.value.expires_at)) : ""));
      const isCur = (pl) => !!(onPlan.value && cur.value.group_id === pl.id);
      const btnText = (pl) => (isCur(pl) ? "续费"
        : onPlan.value ? "换成这张" : "购买");
      /* 卡面规格。只列这张卡真正决定的东西:额度只有「额度限额」策略才有,
         倍率等于 1 不占一行 —— 写 ×1 等于没说。 */
      const specOf = (pl) => {
        const rows = [["计费", POLICY[pl.billing_policy] || pl.billing_policy]];
        if (pl.billing_policy === "quota") {
          const unit = pl.limit_unit === "tokens" ? " Token" : " 次";
          const lim = [["日", pl.daily_limit], ["周", pl.weekly_limit],
            ["月", pl.monthly_limit]].filter((x) => x[1] > 0)
            .map((x) => x[0] + " " + nf(x[1]) + unit);
          rows.push(["额度", lim.length ? lim.join(" · ") : "未设上限"]);
        }
        rows.push(["并发", pl.concurrency_limit ? pl.concurrency_limit + " 个请求" : "不限"]);
        if (Number(pl.rate_multiplier) !== 1)
          rows.push(["倍率", "×" + pl.rate_multiplier]);
        const ms = pl.supported_models || [];
        rows.push(["模型", ms.indexOf("*") >= 0 ? "全部"
          : ms.length ? ms.slice(0, 3).join("、")
            + (ms.length > 3 ? " +" + (ms.length - 3) : "")
          : "未配置"]);
        return rows;
      };

      const buy = (pl) => {
        const renew = isCur(pl);
        dlg.warning({
          title: (renew ? "续费「" : "购买「") + pl.name + "」",
          content: (renew
            ? "从当前到期时间往后加 " + planDur(pl.duration_hours)
              + ",不吞掉你已经付过的那段。"
            : "立即生效 " + planDur(pl.duration_hours) + "。"
              + (onPlan.value
                ? "当前套餐即刻失效,剩余时长不折算、不退款。" : ""))
            + "从余额扣 " + usd(pl.price) + "。",
          positiveText: renew ? "续费" : "购买",
          negativeText: "取消",
          onPositiveClick: () => {
            buying.value = pl.id;
            return api("POST", "/plans/purchase", { group_id: pl.id })
              .then((r) => {
                msg.success("已生效:" + r.plan);
                /* 顶栏那三个 tag(余额/分组/计费方式)是外壳那层拉的,
                   不发这个事件它们会停在买卡之前的口径。 */
                window.dispatchEvent(new Event("bitapi:me-changed"));
                return s.reload();
              }).catch((e) => msg.error(e.message))
              .then(() => { buying.value = 0; });
          },
        });
      };

      const quota = computed(() => {
        const u = b.value.usage || {};
        return [["日", u.daily], ["周", u.weekly], ["月", u.monthly]]
          .filter((x) => x[1]).map((x) => {
            const q = x[1];
            const lim = q.limit || 0;
            return { label: x[0], used: q.used || 0, limit: lim,
              pct: lim > 0 ? Math.min(100, Math.round(q.used / lim * 100)) : 0,
              unlimited: !(lim > 0) };
          });
      });
      const prices = computed(() => b.value.pricing || []);
      const models = computed(() => b.value.supported_models || []);
      const hasLong = computed(() => prices.value.some((r) => r.long_threshold));
      return Object.assign({ b, p, policy, quota, prices, models, hasLong,
        plans, cur, bal, onPlan, left, isCur, btnText, specOf, buy, buying,
        cols: priceCols(), nf, fmt, usd, MONO, POLICY, go, absTime,
        planDur, planKind,
        unitText: computed(() => (b.value.limit_unit === "tokens"
          ? "Token" : "请求次数")) }, s);
    },
    template: `
<Load :loading="loading" :error="error" :on-retry="reload">
<div class="console-page-stack">
<n-alert v-if="!b.group" type="warning" :bordered="false"
  style="font-size:12.5px;margin-bottom:14px">
  这个账号还没有绑定计费分组,现在调用会被拒绝。买一张下面的套餐可以立刻用起来,
  也可以联系管理员分配默认分组。
</n-alert>

<section class="console-hero">
  <div class="console-hero-copy">
    <div class="console-eyebrow">{{ b.group ? '当前分组 · ' + (POLICY[policy] || policy) : '尚未分配计费分组' }}</div>
    <div class="console-hero-title">{{ b.group || '尚未分配分组' }}</div>
    <p class="console-hero-note" v-if="onPlan">{{ cur.name }} · 剩余 {{ left }}
      <template v-if="cur.expires_at"> · {{ absTime(cur.expires_at) }} 到期</template></p>
    <p class="console-hero-note" v-else>{{ p.base_group ? '使用默认分组 ' + p.base_group : '请购买套餐或联系管理员分配分组' }}</p>
    <div class="console-hero-actions"><n-button type="primary" @click="go('#/wallet')">充值余额</n-button>
      <n-button secondary @click="go('#/plaza')">查看模型价格</n-button></div>
  </div>
  <aside class="console-hero-aside">
    <div class="console-inline-stat"><span class="console-inline-stat-label">可用余额</span>
      <strong class="console-inline-stat-value"><MetricValue :value="'$' + fmt(bal)"/></strong></div>
    <div class="console-inline-stat"><span class="console-inline-stat-label">累计消费 · 请求与套餐合计</span>
      <strong class="console-inline-stat-value"><MetricValue :value="'$' + fmt(b.total_spent)"/></strong></div>
  </aside>
</section>

<n-card class="console-panel" size="small" title="订阅套餐">
  <template #header-extra><n-space :size="10" align="center">
    <n-text depth="3" style="font-size:11.5px">余额 \${{ fmt(bal, 2) }}</n-text>
    <n-button size="tiny" secondary @click="go('#/wallet')">充值</n-button>
  </n-space></template>

  <n-alert v-if="cur && !onPlan" type="default" :bordered="false"
    :show-icon="false" style="font-size:12.5px;margin-bottom:12px">
    上次买的「{{ cur.name }}」已在 {{ absTime(cur.expires_at) }} 到期,
    现在按量计费。再买一张会立刻生效。
  </n-alert>

  <n-grid :cols="'1 560:2 1080:3'" :x-gap="12" :y-gap="12" responsive="self">
    <!-- 按量计费不是一张卡,是没买卡时的默认档,所以摆在第一格并标「默认」。 -->
    <n-gi><div class="plan" :class="{on: !onPlan}">
      <n-space :size="6" align="center">
        <span class="planname">按量计费</span>
        <n-tag v-if="!onPlan" size="tiny" round :bordered="false"
          type="primary">当前</n-tag>
        <n-tag size="tiny" round :bordered="false">默认</n-tag>
      </n-space>
      <div><span class="planprice">\$0</span>
        <span class="planunit"> / 无需购买</span></div>
      <div class="plannote">按每次请求的真实用量从余额扣,用多少算多少。
        没买套餐、或套餐到期后都自动落在这一档。</div>
      <div class="planrow" v-if="p.base_group">
        <span class="plank">分组</span>
        <span class="planv">{{ p.base_group }}</span></div>
      <div class="planfoot">
        <n-button v-if="!onPlan" block size="small" secondary disabled>
          当前生效</n-button>
        <n-text v-else depth="3" style="font-size:11.5px">
          套餐到期后自动回到这一档</n-text>
      </div>
    </div></n-gi>

    <n-gi v-for="pl in plans" :key="pl.id">
      <div class="plan" :class="{on: isCur(pl)}">
        <n-space :size="6" align="center">
          <span class="planname">{{ pl.name }}</span>
          <n-tag v-if="isCur(pl)" size="tiny" round :bordered="false"
            type="primary">当前</n-tag>
          <n-tag size="tiny" round :bordered="false" type="info">
            {{ planKind(pl.duration_hours) }}</n-tag>
        </n-space>
        <div><span class="planprice">\${{ fmt(pl.price, 2) }}</span>
          <span class="planunit"> / {{ planDur(pl.duration_hours) }}</span></div>
        <div class="plannote" v-if="pl.notes">{{ pl.notes }}</div>
        <div>
          <div class="planrow" v-for="r in specOf(pl)" :key="r[0]">
            <span class="plank">{{ r[0] }}</span>
            <span class="planv">{{ r[1] }}</span>
          </div>
        </div>
        <div class="planfoot">
          <n-button v-if="pl.price > bal" block size="small" secondary
            @click="go('#/wallet')">余额不足 · 去充值</n-button>
          <n-button v-else block size="small" :secondary="isCur(pl)"
            :type="isCur(pl) ? 'default' : 'primary'"
            :loading="buying === pl.id" @click="buy(pl)">
            {{ btnText(pl) }}</n-button>
        </div>
      </div>
    </n-gi>
  </n-grid>

  <n-text v-if="!plans.length" depth="3"
    style="font-size:12px;display:block;margin-top:12px">
    站点还没上架任何套餐,所有人按量计费。
  </n-text>
  <n-text v-else depth="3" style="font-size:11.5px;display:block;margin-top:12px">
    套餐从余额扣款、按时长生效,到期自动回落按量计费,不自动续费。
    同一张卡在到期前再买是续费叠加(从原到期时间往后加);换成另一张则立即生效,
    原卡剩余时长不折算、不退款。
  </n-text>
</n-card>

<template v-if="b.group">
  <div class="console-section-heading"><div>
    <h2 class="console-section-title">当前生效规则</h2>
    <p class="console-section-note">本分组决定计费方式、调用额度、速率和可用模型。</p>
  </div></div>
  <n-grid :cols="'1 1000:2'" :x-gap="14" :y-gap="14" responsive="self">
    <n-gi><n-card size="small" title="套餐详情">
      <template #header-extra><n-tag size="small" round :bordered="false"
        type="primary">{{ b.group }}</n-tag></template>
      <n-descriptions :column="1" label-placement="left" size="small"
        :label-style="{opacity:.62,width:'76px'}">
        <n-descriptions-item label="计费方式">
          <n-tag size="small" round :bordered="false"
            :type="policy === 'balance' ? 'info' : policy === 'quota' ? 'warning' : 'success'">
            {{ POLICY[policy] || policy }}</n-tag>
        </n-descriptions-item>
        <n-descriptions-item v-if="onPlan" label="有效期">
          {{ left }}<n-text v-if="cur.expires_at" depth="3"
            style="font-size:11.5px"> · {{ absTime(cur.expires_at) }} 到期
          </n-text>
        </n-descriptions-item>
        <n-descriptions-item label="倍率">
          <span :style="MONO">×{{ b.rate_multiplier }}</span>
        </n-descriptions-item>
        <n-descriptions-item label="并发上限">
          <span :style="MONO">{{ b.concurrency_limit || '不限' }}</span>
        </n-descriptions-item>
        <n-descriptions-item label="限额口径">
          {{ unitText }}</n-descriptions-item>
        <n-descriptions-item label="可用模型">
          <n-space :size="4" style="max-width:420px">
            <n-tag v-for="m in models" :key="m" size="small" round
              :bordered="false" :style="MONO">{{ m }}</n-tag>
            <n-text v-if="!models.length" depth="3">无</n-text>
          </n-space>
        </n-descriptions-item>
      </n-descriptions>
      <template #action v-if="policy === 'balance'">
        <n-button size="small" text type="primary" @click="go('#/wallet')">
          去充值 →</n-button>
      </template>
    </n-card></n-gi>

    <n-gi>
      <n-card v-if="policy === 'quota'" size="small"
        :title="'额度用量 · 按' + unitText">
        <n-space vertical :size="16">
          <div v-for="q in quota" :key="q.label">
            <n-space justify="space-between" style="margin-bottom:5px">
              <n-text style="font-size:13px">{{ q.label }}</n-text>
              <n-text :style="MONO" style="font-size:12.5px">
                {{ nf(q.used) }}<template v-if="!q.unlimited">
                 / {{ nf(q.limit) }}</template>
                <n-text v-else depth="3"> · 不限</n-text>
              </n-text>
            </n-space>
            <n-progress type="line" :percentage="q.pct" :height="7"
              :show-indicator="false" :border-radius="99"
              :status="q.unlimited ? 'default' : q.pct >= 90 ? 'error'
                : q.pct >= 70 ? 'warning' : 'success'"/>
          </div>
        </n-space>
        <n-text depth="3" style="font-size:11.5px;display:block;margin-top:14px">
          额度按滚动窗口统计(近 1/7/30 天),不在自然日边界清零;超限只拦请求,不扣费。
        </n-text>
      </n-card>

      <n-card v-else-if="policy === 'free'" size="small" title="免费套餐">
        <n-space vertical :size="10">
          <n-text style="font-size:13px">当前套餐不计费,只受速率与模型范围限制。</n-text>
          <n-text depth="3" style="font-size:12px">
            使用记录仍会完整落库,费用一栏显示为「免费」,方便日后切到余额扣费时对账。
          </n-text>
        </n-space>
      </n-card>

      <n-card v-else size="small" title="扣费规则">
        <n-space vertical :size="8">
          <n-text style="font-size:12.5px">
            单价以美元 / 1M token 计,输入、输出、缓存读、缓存写各算各的价,
            合计后再乘分组倍率 ×{{ b.rate_multiplier }}。
            上游返回 30 分钟 / 12 小时写入分档时，分别按基础缓存写价 / 2 倍基础缓存写价计费。
          </n-text>
          <n-text style="font-size:12.5px" v-if="hasLong">
            带长文本阈值的模型按「整次跳档」计价:判定量
            (输入 + 缓存写 + 缓存读,不含输出) 严格大于阈值时,
            本次全部 token 换用长档单价,不是只有超出部分变贵。
          </n-text>
          <n-text depth="3" style="font-size:11.5px">
            每次请求都会把当时生效的单价冻结进快照,后续改价不影响历史账单。
          </n-text>
        </n-space>
      </n-card>
    </n-gi>
  </n-grid>

  <n-card v-if="policy === 'balance'" class="console-table-card" size="small"
    title="生效单价 · 美元 / 1M token">
    <template #header-extra><n-text depth="3" style="font-size:11.5px">
      {{ prices.length }} 条</n-text></template>
    <n-alert v-if="!prices.length" type="default" :bordered="false">
      未配置专属定价,按系统内置价目表计费;价目表也缺价时该模型免费(不会因缺价拒绝请求)。
    </n-alert>
    <template v-else>
      <n-data-table :columns="cols" :data="prices" size="small" :bordered="false"
        :single-line="false" :scroll-x="930" :max-height="440" virtual-scroll
        :row-key="(r) => r.model_pattern"/>
      <n-text v-if="hasLong" depth="3"
        style="font-size:11.5px;display:block;margin-top:10px">
        单元格上下两行分别是普通档与长文本档单价;只显示一行说明该项未配长档,
        跳档时这一项单独回落普通价。
      </n-text>
    </template>
  </n-card>
</template>
</div>
</Load>`,
  };
  /* 官方品牌图标要在模板里用,包一层组件;和 Ic 对 ICONS 的关系一样。 */
  const PIcon = { props: ["model", "size"],
    render() { return provIcon(this.model, this.size); } };

  /* 模型广场:能调的模型 × 生效单价。可见范围由后端按分组白名单裁过,
     和 /v1/models 同一套判断 —— 这里看得到的,拿密钥就一定调得通。 */
  const MODE_TAG = { token: "info", per_request: "warning", free: "success",
    mixed: "default" };
  const SRC_TEXT = { group: "分组专属价", global: "全局价",
    builtin: "内置目录价", litellm: "LiteLLM 目录价",
    fallback: "未配价(免费)" };
  const SORT_OPTS = [{ value: "name", label: "按名称" },
    { value: "price-asc", label: "价格从低到高" },
    { value: "price-desc", label: "价格从高到低" }];

  /* 卡面价格摘要:一行说清这张卡怎么收钱。区间只在折叠卡里出现。 */
  function priceBrief(r) {
    if (r.billing_mode === "token")
      return "$" + fmt(r.input_price) + " 入 · $" + fmt(r.output_price) + " 出 / 1M Token";
    if (r.billing_mode === "free") return "免费";
    const lo = r.per_request_price || 0;
    const hi = r.per_request_price_max;
    if (hi != null && hi !== lo)
      return "$" + fmt(lo) + " ~ $" + fmt(hi) + " / 次";
    return "$" + fmt(lo) + " / 次";
  }

  /* 排序键:按 Token 取输入价(卡面第一个数,也是口语里说的「多少钱」),
     按次取每次价,免费按 0。不去合成 input+output 那种复合分数 ——
     那个数在界面上任何地方都不显示,排出来的顺序就没法自己解释。 */
  function priceKey(r) {
    return r.billing_mode === "token" ? (r.input_price || 0)
      : (r.per_request_price || 0);
  }

  const Plaza = {
    components: { Load, SectionHead, Ic, PIcon },
    setup() {
      const d = ref({ data: [], rate_multiplier: 1 });
      const s = useFetch(() => api("GET", "/models").then((r) => {
        d.value = r;
      }));
      /* 巡检状态条(对话卡):绿=当天全部通过 红=有失败 黄=号池空 灰=没测。
         生成类(图/视频)不巡检,卡片不带这一行。 */
      const BAR_C = { 1: "#18a058", 0: "#de5764", 2: "#f0a020" };
      const BAR_T = { 1: "正常", 0: "不能用", 2: "慢(>10s)" };
      const SLOW_MS = 10000;
      const gradeOfSt = (st) => st.ok == null ? null
        : (st.ok !== 1 ? 0 : ((st.ttft_ms || st.latency_ms || 0) > SLOW_MS ? 2 : 1));
      const ST_TYPE = { 1: "success", 2: "warning", 0: "error" };
      const ST_TEXT = { 1: "正常", 2: "偏慢", 0: "故障" };
      const ttftText = (ms) => !ms ? "—"
        : ms >= 1000 ? (ms / 1000).toFixed(1) + "s" : ms + "ms";
      const hbSlots = (o) => (o && o.heartbeats && o.heartbeats.length)
        ? o.heartbeats : (o && o.bars) || [];
      const barsCell = (st) => h(naive.NSpace, { size: 2, wrap: false,
        align: "center" }, () => hbSlots(st).map((b) =>
        h(naive.NTooltip, {}, {
          trigger: () => h("div", { style: {
            width: "7px", height: "16px", borderRadius: "2px",
            background: b.grade === null || b.grade === undefined
              ? "#e0e0e6" : (BAR_C[b.grade] || "#e0e0e6") } }),
          default: () => h("span", { style: MONO },
            (b.label || b.date) + " " + (BAR_T[b.grade] || "没测")) })));
      const mutedSmall = (t) => h("span", { style: Object.assign(
        { fontSize: "11.5px" }, MONO) }, t);
      const q = ref("");
      const fv = ref(null);
      const fm = ref(null);
      const view = ref("card");
      const sort = ref("name");
      const detail = ref(null);

      const rows = computed(() => (d.value.data || []).map((r) =>
        Object.assign({ p: prov(r.model) }, r)));
      /* 筛选项带条数:选之前就知道点进去有几个,不用试。
         iconOf 返回该项要用的 prov 对象 —— 必须由行上已解析好的 r.p 带过来,
         不能拿选项文字去重新 prov() 一次:供应商名和模型名不是一套词,
         "OpenAI" / "Google" / "Zhipu" 这些名字反查规则表全都对不上,
         下拉里会退化成字母牌,跟卡片上的图标两个样。 */
      const opts = (key, iconOf) => {
        const n = {};
        const icon = {};
        rows.value.forEach((r) => {
          const v = typeof key === "function" ? key(r) : r[key];
          n[v] = (n[v] || 0) + 1;
          if (iconOf && !(v in icon)) icon[v] = iconOf(r);
        });
        return Object.keys(n).sort().map((k) => ({ value: k,
          label: k + " (" + n[k] + ")", icon: icon[k] }));
      };
      const vendorOpts = computed(() => opts((r) => r.p.n, (r) => r.p));
      const modeOpts = computed(() => opts("billing_mode").map((o) => ({
        value: o.value, label: (MODE[o.value] || "混合") +
          o.label.slice(o.label.indexOf(" ")) })));
      /* 下拉项的渲染:带 icon 的画「图 + 文字」,不带的还给 Naive 画默认文字。 */
      const renderOpt = (o) => (o && o.icon
        ? h("div", { style: { display: "flex", alignItems: "center",
            gap: "7px" } }, [provIcon(o.icon, 15), h("span", null, o.label)])
        : (o && o.label));

      const list = computed(() => {
        const kw = q.value.trim().toLowerCase();
        const out = rows.value.filter((r) =>
          (!fv.value || r.p.n === fv.value) &&
          (!fm.value || r.billing_mode === fm.value) &&
          (!kw || r.model.toLowerCase().indexOf(kw) >= 0 ||
            r.p.n.toLowerCase().indexOf(kw) >= 0));
        if (sort.value === "name")
          return out.slice().sort((a, b) => a.model.localeCompare(b.model));
        /* 按价排序只比数字。按 Token 是「美元 / 1M」、按次是「美元 / 次」,
           两种量纲混在一张榜上本来就不可比 —— 单价列始终带单位,量纲差异看得见,
           真要单一量纲的榜用「计费方式」筛一下即可。 */
        const dir = sort.value === "price-desc" ? -1 : 1;
        return out.slice().sort((a, b) =>
          (priceKey(a) - priceKey(b)) * dir || a.model.localeCompare(b.model));
      });
      const reset = () => {
        q.value = "";
        fv.value = fm.value = null;
        sort.value = "name";
      };

      /* 抽屉里的单价明细:只列真正有值的项。长档单价与普通档并排,
         缺长档的项不显示第二行 —— 跳档时那一项本来就单独回落普通价。 */
      const priceRows = computed(() => {
        const r = detail.value;
        if (!r || r.billing_mode !== "token") return [];
        const items = [["输入", "input_price", "long_input_price"],
          ["输出", "output_price", "long_output_price"],
          ["缓存读", "cache_read_price", "long_cache_read_price"],
          ["缓存写", "cache_write_price", "long_cache_write_price"]];
        return items.filter((it) => r[it[1]] || r[it[2]]).map((it) => ({
          label: it[0], base: r[it[1]] || 0,
          long: r[it[2]] == null ? null : r[it[2]] }));
      });
      /* 折叠卡的卡名不是可调用名 —— 能填进 model 字段的是各规格全名。
         这里把可调用名单独列出来,避免用户抄了卡名去调然后吃 404。 */
      const callable = computed(() => {
        const r = detail.value;
        if (!r) return [];
        return r.variants && r.variants.length
          ? r.variants.map((v) => v.model) : [r.model];
      });

      /* 表格列。排序统一交给工具条那个 sort,列头不再挂 sorter ——
         两套排序入口摆在一起,用户点了列头就再也说不清当前按什么排的。 */
      const cols = [
        { title: "模型", key: "model", minWidth: 264, render: (r) =>
            h("div", { style: { display: "flex", alignItems: "center",
              gap: "7px" } }, [provIcon(r.p, 17),
              h("span", { style: Object.assign({ fontSize: "12.5px",
                fontWeight: 600 }, MONO) }, r.model)]) },
        { title: "供应商", key: "vendor", width: 108,
          render: (r) => r.p.n },
        { title: "状态", key: "status", width: 190, render: (r) =>
            r.status ? barsCell(r.status) : mutedSmall("不巡检") },
        { title: "计费方式", key: "billing_mode", width: 104, render: (r) =>
            tag(MODE_TAG[r.billing_mode] || "default",
              MODE[r.billing_mode] || "混合计费") },
        { title: "单价", key: "price", minWidth: 196, render: (r) =>
            h("span", { style: Object.assign({ fontSize: "12.5px",
              fontWeight: 600 }, MONO) },
              r.billing_mode === "free"
                ? (r.price_source === "fallback" ? "未配单价" : "免费")
                : priceBrief(r)) },
        { title: "规格", key: "variants", width: 74, align: "right",
          render: (r) => (r.variants.length
            ? h("span", MONO_ATTR, r.variants.length) : "—") },
        { title: "", key: "actions", width: 88, render: (r) =>
            h(naive.NButton, { text: true, type: "primary",
              onClick: (e) => { e.stopPropagation(); detail.value = r; },
              "aria-label": "查看 " + r.model + " 详情" }, () => "详情") },
      ];
      const rowProps = (r) => ({ style: "cursor:pointer",
        onClick: () => { detail.value = r; } });

      return Object.assign({ d, q, fv, fm, view, sort, detail, list, reset,
        vendorOpts, modeOpts, priceRows, callable, cols, rowProps,
        renderOpt, MODE, MODE_TAG, SRC_TEXT, SORT_OPTS, MONO, SUBSTY, fmt, copy,
        priceBrief, go, BAR_C, BAR_T, ST_TYPE, ST_TEXT, ttftText, barsCell,
        gradeOfSt,
        total: computed(() => rows.value.length) }, s);
    },
    template: `
<Load :loading="loading" :error="error" :on-retry="reload">
<div class="console-page-stack">
<section class="console-hero">
  <div class="console-hero-copy">
    <div class="console-eyebrow">当前可见的模型目录</div>
    <div class="console-hero-title"><MetricValue :value="total + ' 项模型'" animate/></div>
    <p class="console-hero-note">按供应商与计费方式探索。多种规格合并展示，可在详情中查看各规格的单价与调用名称。</p>
    <div class="console-hero-actions"><n-button type="primary" @click="go(d.guest ? '#/login?next=playground' : '#/playground')">
      {{ d.guest ? '登录/注册后体验' : '在线体验对话模型' }}</n-button>
      <n-button secondary @click="go(d.guest ? '#/login?next=keys' : '#/keys')">
        {{ d.guest ? '登录/注册后接入' : '获取 API 密钥' }}</n-button></div>
  </div>
  <aside class="console-hero-aside">
    <div class="console-inline-stat"><span class="console-inline-stat-label">{{ d.guest ? '默认分组' : '当前分组' }}</span>
      <strong class="console-inline-stat-value">{{ d.group || '—' }}</strong></div>
    <p v-if="d.policy === 'free'" class="console-section-note">当前分组不计费，下方展示模型原始单价供参考。</p>
    <p v-else-if="d.policy === 'quota'" class="console-section-note">当前分组按额度限额，下方展示模型原始单价供参考。</p>
    <p v-else class="console-section-note">显示原始单价，结算再乘分组倍率 ×{{ d.rate_multiplier }}。</p>
  </aside>
</section>
<section class="console-panel">
  <div class="console-toolbar">
    <n-input v-model:value="q" clearable placeholder="搜模型 / 供应商"
      style="width:224px"/>
    <n-select v-model:value="fv" :options="vendorOpts" clearable
      placeholder="供应商" style="width:168px" :render-label="renderOpt"/>
    <n-select v-model:value="fm" :options="modeOpts" clearable
      placeholder="计费方式" style="width:150px"/>
    <n-select v-model:value="sort" :options="SORT_OPTS" style="width:132px"/>
    <n-button v-if="q || fv || fm || sort !== 'name'" quaternary
      size="small" @click="reset">清空</n-button>
  </div>
  <!-- 游客看的是默认分组的价与可见范围。不写出来的话,注册后落到别的组、
       价格对不上,只会被当成标错价。 -->
  <n-alert v-if="d.guest" type="default" :bordered="false" :show-icon="false"
    style="margin-top:10px;font-size:12px">
    未登录,按默认分组「{{ d.group || '—' }}」的价格与可见范围显示。
    注册后如果被分到别的分组,单价与可用模型会跟着变。
  </n-alert>
</section>

<div class="console-section-heading">
  <div><h2 class="console-section-title">{{ q || fv || fm ? '筛选结果' : '全部模型' }}</h2>
    <p class="console-section-note">{{ list.length }} / {{ total }} 个模型 · 按 Token 与按次的价格单位不同，可按计费方式筛选后比较。</p></div>
  <n-radio-group v-model:value="view" size="small">
    <n-radio-button value="card">卡片</n-radio-button>
    <n-radio-button value="table">表格</n-radio-button>
  </n-radio-group>
</div>

<n-empty v-if="!list.length" :description="q || fv || fm ? '没有匹配的模型，请调整筛选条件' : '当前分组暂无可用模型'"
  style="padding:52px 0"><template #extra><n-button v-if="q || fv || fm" secondary @click="reset">清空筛选</n-button></template></n-empty>
<n-grid v-else-if="view === 'card'" :cols="'1 620:2 1060:3'" :x-gap="16"
  :y-gap="16" responsive="self">
  <n-gi v-for="r in list" :key="r.model">
    <n-card class="console-model-card" size="small" style="height:100%">
      <div style="display:flex;align-items:center;gap:8px">
        <PIcon :model="r.model" :size="28"/>
        <div style="min-width:0;flex:1">
          <div :style="[MONO, {fontSize:'14px',fontWeight:650,overflowWrap:'anywhere'}]"
            :title="r.model">{{ r.model }}</div>
          <div :style="SUBSTY" style="opacity:.6;margin-top:2px">
            {{ r.p.n }}</div>
        </div>
      </div>
      <div class="console-model-meta">
        <n-tag size="small" round :bordered="false"
          :type="MODE_TAG[r.billing_mode] || 'default'">
          {{ MODE[r.billing_mode] || '混合计费' }}</n-tag>
        <strong v-if="r.billing_mode !== 'free'" :style="MONO">
          {{ priceBrief(r) }}</strong>
        <n-text v-else-if="r.price_source === 'fallback'" depth="3"
          style="font-size:11.5px">未配单价</n-text>
      </div>
      <div v-if="r.status" style="display:flex;align-items:center;gap:9px;margin-top:9px">
        <n-space :size="3" :wrap="false" align="center">
          <n-tooltip v-for="b in r.status.bars" :key="b.date">
            <template #trigger>
              <div :style="{ width: '9px', height: '16px', borderRadius: '2px',
                background: b.grade === null ? '#e0e0e6' : (BAR_C[b.grade] || '#e0e0e6') }"/>
            </template>
            <span :style="MONO" style="font-size:12px">
              {{ b.date + ' ' + (BAR_T[b.grade] || '没测') }}</span>
          </n-tooltip>
        </n-space>
        <n-tag size="small" round :bordered="false"
          :type="ST_TYPE[gradeOfSt(r.status)] || 'default'">
          {{ ST_TEXT[gradeOfSt(r.status)] || '未检测' }}</n-tag>
        <n-text v-if="r.status.ttft_ms" depth="3" :style="MONO"
          style="font-size:11.5px">首字 {{ ttftText(r.status.ttft_ms) }}</n-text>
      </div>
      <p class="console-section-note">{{ r.variants.length ? r.variants.length + ' 种规格 · 查看对应调用名与单价' : '可直接使用模型名称接入' }}</p>
      <template #action><n-space justify="space-between" align="center">
        <n-button text type="primary" @click="detail = r" :aria-label="'查看 ' + r.model + ' 详情'">查看详情</n-button>
        <n-button quaternary size="small" @click="copy(r.variants.length ? r.variants[0].model : r.model, '已复制模型名')">
          <template #icon><Ic name="copy" :size="14"/></template>{{ r.variants.length ? '复制首个规格' : '复制名称' }}</n-button>
      </n-space></template>
    </n-card>
  </n-gi>
</n-grid>

<n-data-table v-else :columns="cols" :data="list" :bordered="false" size="small"
  :single-line="false" :scroll-x="1100" :row-key="(r) => r.model"
  :row-props="rowProps"
  :pagination="{pageSize:20,pageSlot:5,showSizePicker:true,
    pageSizes:[20,50,100],prefix:({itemCount})=>'总计 '+itemCount}"/>

</div>

<n-drawer :show="!!detail" width="min(500px, 100vw)" placement="right"
  @update:show="v => { if (!v) detail = null }">
  <n-drawer-content v-if="detail" closable :native-scrollbar="false"
    :title="detail.model">
    <n-space align="center" :size="9" style="margin:-4px 0 2px">
      <PIcon :model="detail.model" :size="18"/>
      <n-text depth="3" style="font-size:12.5px">
        {{ detail.p.n }}</n-text>
      <n-tag size="small" round :bordered="false"
        :type="MODE_TAG[detail.billing_mode] || 'default'">
        {{ MODE[detail.billing_mode] || '混合计费' }}</n-tag>
    </n-space>

    <n-divider style="margin:16px 0 14px"/>
    <SectionHead icon="dollar" title="生效单价"
      :sub="(SRC_TEXT[detail.price_source] || '各规格来源不一') +
        ' · 结算再乘分组倍率 ×' + d.rate_multiplier" color="#4a6fa5"/>

    <n-descriptions v-if="priceRows.length" :column="1" size="small"
      label-placement="left" style="margin-top:12px">
      <n-descriptions-item v-for="p in priceRows" :key="p.label"
        :label="p.label">
        <span :style="MONO">{{ '$' + fmt(p.base) }} / 1M</span>
        <n-text v-if="p.long != null" depth="3"
          :style="[MONO, {fontSize:'11.5px',marginLeft:'8px'}]">
          长档 {{ '$' + fmt(p.long) }}</n-text>
      </n-descriptions-item>
    </n-descriptions>
    <n-alert v-else-if="detail.billing_mode === 'free'" type="success"
      :bordered="false" :show-icon="false" style="margin-top:12px">
      未配置单价,按免费计 —— 网关不会因缺价拒绝请求。
    </n-alert>
    <n-text v-else :style="[MONO, {fontSize:'13px',fontWeight:650,
      display:'block',marginTop:'12px'}]">{{ priceBrief(detail) }}</n-text>

    <n-text v-if="detail.long_threshold" depth="3"
      style="font-size:11.5px;display:block;margin-top:9px">
      输入 + 缓存合计超过 {{ fmt(detail.long_threshold, 0) }} token 时整次换长档;
      某项没配长档价,该项单独回落普通价。
    </n-text>

    <template v-if="detail.variants.length">
      <n-divider style="margin:18px 0 14px"/>
      <SectionHead icon="sliders" title="规格与单价"
        sub="分辨率、时长不同价,按次计费" color="#8a6d3b"/>
      <n-space vertical :size="6" style="margin-top:12px">
        <div v-for="v in detail.variants" :key="v.model"
          style="display:flex;align-items:center;gap:8px">
          <span :style="[MONO, {fontSize:'12px',flex:1,minWidth:0,
            overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}]"
            :title="v.model">{{ v.spec }}</span>
          <span :style="[MONO, {fontSize:'12px',fontWeight:600}]">
            {{ v.billing_mode === 'free' ? '免费'
               : '$' + fmt(v.per_request_price) + ' / 次' }}</span>
          <n-button quaternary circle size="tiny"
            @click="copy(v.model, '已复制模型名')">
            <Ic name="copy" :size="13"/></n-button>
        </div>
      </n-space>
    </template>

    <n-divider style="margin:18px 0 14px"/>
    <SectionHead icon="cube" title="怎么调用"
      :sub="detail.variants.length
        ? '卡片标题只是归类名,填进 model 的必须是下面这些全名'
        : '把这个名字填进请求的 model 字段'" color="#4a6fa5"/>
    <n-space vertical :size="6" style="margin-top:12px">
      <n-input-group v-for="name in callable" :key="name">
        <n-input :value="name" readonly/>
        <n-button secondary @click="copy(name, '已复制模型名')">复制</n-button>
      </n-input-group>
    </n-space>
  </n-drawer-content>
</n-drawer>
</Load>`,
  };

  /* ---- 在线体验 ----
     一次体验就是一次真实网关请求:走 /api/playground/chat(JWT 鉴权),
     计费与 /v1/chat/completions 同路,消费如实进「使用记录」。
     所以这里不做任何"试用额度"的概念,余额不足就跟真调用一样被 402 拦住。 */

  /* 把 SSE 流读成一段段增量。fetch + ReadableStream 而不是 EventSource:
     EventSource 只能 GET、不能带 Authorization 头。 */
  async function readSSE(res, onDelta, onUsage) {
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      /* 按空行切帧;最后一段可能不完整,留在 buf 里等下一片。 */
      const parts = buf.split("\n\n");
      buf = parts.pop();
      for (const part of parts) {
        const line = part.split("\n").find((l) => l.startsWith("data:"));
        if (!line) continue;
        const raw = line.slice(5).trim();
        if (raw === "[DONE]") return;
        let obj;
        try { obj = JSON.parse(raw); } catch (e) { continue; }
        if (obj.usage) onUsage(obj.usage);
        const d = ((obj.choices || [{}])[0] || {}).delta || {};
        if (d.content) onDelta(d.content);
        /* 推理模型把思维链放在 reasoning_content,单独收,不混进正文。 */
        if (d.reasoning_content) onDelta(d.reasoning_content, true);
      }
    }
  }

  const Playground = {
    components: { Load, SectionHead, Ic, PIcon },
    setup() {
      const models = ref([]);
      const model = ref("");
      const s = useFetch(() => api("GET", "/models").then((r) => {
        /* 只留对话模型:chat 位由渠道的 CAP_CHAT 声明,图/视频渠道进不来。
           折叠卡的卡名不是可调用名 —— 有 variants 就展开成每个规格的调用名,
           否则选中后发给 /api/playground/chat 会吃 404 unknown model。 */
        const out = [];
        const seen = new Set();
        for (const m of (r.data || [])) {
          if (!m.chat) continue;
          const names = (m.variants && m.variants.length)
            ? m.variants.map((v) => v.model) : [m.model];
          for (const name of names) {
            if (seen.has(name)) continue;
            seen.add(name);
            out.push(Object.assign({}, m, { model: name }));
          }
        }
        models.value = out;
        if (!model.value && models.value.length)
          model.value = models.value[0].model;
      }));

      const turns = ref([]);      // {role, text, reasoning, frt, ms, usage, err}
      const draft = ref("");
      const busy = ref(false);
      let abort = null;

      const opts = computed(() => models.value.map((m) => ({
        value: m.model, label: m.model, icon: prov(m.model) })));
      const renderOpt = (o) => (o && o.icon
        ? h("div", { style: { display: "flex", alignItems: "center",
            gap: "7px" } }, [provIcon(o.icon, 15),
            h("span", null, o.label)])
        : (o && o.label));

      const stop = () => { if (abort) abort.abort(); };
      const clear = () => { if (!busy.value) turns.value = []; };

      const send = async () => {
        const text = draft.value.trim();
        if (!text || busy.value) return;
        if (!model.value) return msg.warning("先选一个模型");
        draft.value = "";
        turns.value.push({ role: "user", text: text });
        /* 整段历史一起发:体验的是多轮对话,只发最后一句就不是同一件事。
           历史越长 input token 越多、花费也越多 —— 和真接进去一模一样。 */
        const history = turns.value
          .filter((t) => t.role === "user" || t.text)
          .map((t) => ({ role: t.role, content: t.text }));
        const cur = reactive({ role: "assistant", text: "", reasoning: "",
                               frt: null, ms: null, usage: null, err: "" });
        turns.value.push(cur);
        busy.value = true;
        abort = new AbortController();
        const t0 = Date.now();
        try {
          const res = await fetch(BASE + "/api/playground/chat", {
            method: "POST", signal: abort.signal,
            headers: { "Content-Type": "application/json",
                       ...P.authHeaders() }, credentials: "same-origin",
            body: JSON.stringify({ model: model.value, messages: history }),
          });
          if (!res.ok) {
            let d = null;
            try { d = (await res.json()).detail; } catch (e) {}
            throw new Error(d && typeof d === "object"
              ? (d.message || JSON.stringify(d)) : (d || res.status));
          }
          await readSSE(res, (delta, isReasoning) => {
            if (cur.frt == null) cur.frt = Date.now() - t0;
            if (isReasoning) cur.reasoning += delta;
            else cur.text += delta;
          }, (u) => { cur.usage = u; });
        } catch (e) {
          cur.err = e.name === "AbortError" ? "已停止" : (e.message || String(e));
        } finally {
          cur.ms = Date.now() - t0;
          busy.value = false;
          abort = null;
          /* 这一轮的钱已经扣了,让顶栏余额跟着变,否则要刷新才看得到。 */
          window.dispatchEvent(new Event("bitapi:me-changed"));
        }
      };

      onUnmounted(stop);
      return Object.assign({ models, model, opts, renderOpt, turns, draft,
        busy, send, stop, clear, MONO, SUBSTY, kf, nf, go,
        selected: computed(() => models.value.find((m) => m.model === model.value)),
        priceBrief,
        prompts: [
          { label: "理解一个概念", note: "观察解释是否清晰、准确。", text: "请用一个具体例子解释什么是 API，以及它如何连接两个应用。" },
          { label: "整理一段思路", note: "把输入变成清晰的执行步骤。", text: "我准备给应用接入 AI 对话功能，请帮我列出从原型到上线最关键的五个步骤。" },
          { label: "检查一段代码", note: "粘贴代码，验证分析与建议。", text: "请帮我检查下面的代码，指出潜在错误并解释如何修改：\n\n" },
        ] }, s);
    },
    template: `
<Load :loading="loading" :error="error" :on-retry="reload">
<div class="console-page-stack">
<n-card class="console-panel" size="small">
  <div class="console-section-heading">
    <div><h2 class="console-section-title">当前对话</h2>
      <p class="console-section-note">选择模型后发送消息。每次发送为真实请求，按当前分组计费。</p></div>
    <n-button secondary @click="go('#/usage')">查看使用记录</n-button>
  </div>
  <div class="console-toolbar">
    <n-select v-model:value="model" :options="opts" filterable
      :disabled="busy" placeholder="选择模型" style="width:300px"
      :render-label="renderOpt"/>
    <n-button size="small" quaternary :disabled="busy || !turns.length"
      @click="clear">清空对话</n-button>
    <n-text v-if="selected" depth="3">原始单价 {{ priceBrief(selected) }}</n-text>
  </div>
  <n-alert v-if="!models.length" type="warning" :bordered="false"
    :show-icon="false" style="margin-top:10px;font-size:12px">
    当前分组没有可用的对话模型。图片和视频模型走生成接口,不在这里体验。
  </n-alert>
</n-card>

<section class="console-playground-conversation" aria-label="对话内容">
<div v-if="!turns.length" class="console-playground-empty">
  <div class="console-eyebrow">从一个问题开始</div>
  <h2 class="console-section-title">看看模型如何处理你的任务</h2>
  <p class="console-section-note">选择下方示例填入输入框，也可以直接写下自己的问题。</p>
  <div class="console-action-grid">
    <button v-for="p in prompts" :key="p.label" type="button" class="console-action-card"
      :disabled="!models.length" @click="draft = p.text">
      <strong>{{ p.label }}</strong><span>{{ p.note }}</span>
    </button>
  </div>
</div>
<n-space v-else vertical :size="20">
  <div v-for="(t, i) in turns" :key="i"
    :style="{display:'flex', justifyContent: t.role === 'user'
      ? 'flex-end' : 'flex-start'}">
    <div :style="{maxWidth:'760px', minWidth:0}">
      <n-card size="small" :bordered="false"
        :content-style="'padding:11px 13px'"
        class="console-chat-message" :class="{'console-chat-user':t.role === 'user'}">
        <div class="console-eyebrow" style="margin-bottom:8px">{{ t.role === 'user' ? '你' : '模型回复' }}</div>
        <n-collapse v-if="t.reasoning" style="margin:-4px 0 8px">
          <n-collapse-item title="思考过程" name="r">
            <div :style="[SUBSTY, {whiteSpace:'pre-wrap',opacity:.7}]"
              >{{ t.reasoning }}</div>
          </n-collapse-item>
        </n-collapse>
        <div style="white-space:pre-wrap;word-break:break-word;font-size:13.5px;
          line-height:1.65">{{ t.text }}<span
          v-if="busy && t.role === 'assistant' && i === turns.length - 1"
          style="opacity:.5">▍</span></div>
        <n-alert v-if="t.err" type="error" :bordered="false" :show-icon="false"
          style="margin-top:8px;font-size:12px">{{ t.err }}</n-alert>
      </n-card>
      <div v-if="t.role === 'assistant' && t.ms != null" :style="SUBSTY"
        style="opacity:.5;margin:5px 0 0 3px">
        首字 {{ t.frt == null ? '—' : t.frt + ' ms' }} ·
        用时 {{ (t.ms / 1000).toFixed(1) }} s<template v-if="t.usage">
        · 入 {{ kf(t.usage.prompt_tokens || 0) }} /
        出 {{ kf(t.usage.completion_tokens || 0) }} token</template>
      </div>
    </div>
  </div>
</n-space>
</section>

<n-card class="console-chat-composer" size="small" :bordered="false">
  <n-space vertical :size="8">
    <n-input v-model:value="draft" type="textarea" :autosize="{minRows:3,maxRows:8}"
      :disabled="!models.length" :input-props="{'aria-label':'对话消息'}"
      placeholder="说点什么… Ctrl/⌘ + Enter 发送"
      @keydown.ctrl.enter="send" @keydown.meta.enter="send"/>
    <n-space justify="space-between" align="center" :size="8">
      <n-text depth="3" style="font-size:12px">多轮对话会携带此前消息，历史越长，输入用量越多。</n-text>
      <n-space :size="8">
      <n-button v-if="busy" size="small" @click="stop">停止</n-button>
      <n-button type="primary" :disabled="busy || !draft.trim() || !models.length"
        :loading="busy" @click="send">发送</n-button>
      </n-space>
    </n-space>
  </n-space>
</n-card>
</div>
</Load>`,
  };

  /* ---------------- 模型状态(用户侧) ---------------- */

  const ModelStatus = {
    components: { Load, StatCard },
    setup() {
      const rows = ref([]);
      const interval = ref(3600);
      const summary = ref({});
      const filter = ref("");
      const view = computed(() => {
        const value = filter.value.trim().toLowerCase();
        if (!value) return rows.value;
        return rows.value.filter((r) =>
          (r.model + " " + (r.channels || []).join(" ")).toLowerCase().includes(value));
      });
      const s = useFetch(() => api("GET", "/model-status").then((r) => {
        rows.value = r.rows || [];
        interval.value = r.interval || 3600;
        summary.value = r.summary || {};
      }));
      /* 状态页看的就是「现在怎么样」,60 秒一刷保底;管理员在后台手动触发
         一轮新检测时,这边也会在一分钟内跟上。 */
      let timer;
      onMounted(() => { timer = setInterval(() => s.reload(), 60000); });
      onUnmounted(() => clearInterval(timer));
      /* 状态条:红=不可用 黄=通过但首字超 10s 绿=正常 灰=没测(用户视角取最好) */
      const SLOW_MS = 10000;
      const gradeOf = (r) => r.ok == null ? null
        : (r.ok !== 1 ? 0 : ((r.ttft_ms || r.latency_ms || 0) > SLOW_MS ? 2 : 1));
      const stTag = (r) => {
        const g = gradeOf(r);
        if (g === 1) return tag("success", "正常");
        if (g === 2) return tag("warning", "偏慢");
        if (g === 0) return tag("error", "故障");
        return tag("default", "未检测");
      };
      const BAR_C = { 1: "#18a058", 0: "#de5764", 2: "#f0a020" };
      const barText = { 1: "正常", 0: "不可用", 2: "慢(>10s)" };
      /* Plaza 里那份 hbSlots 是组件内局部函数,这里拿不到,必须自带一份;
         漏了它就是 ReferenceError,整张表渲染不出来(24 小时格子全空)。 */
      const hbSlots = (o) => (o && o.heartbeats && o.heartbeats.length)
        ? o.heartbeats : (o && o.bars) || [];
      const barsCell = (r) => h(naive.NSpace, { size: 2, wrap: false, align: "center" },
        () => hbSlots(r).map((b) => h(naive.NTooltip, {}, {
          trigger: () => h("div", { style: {
            width: "7px", height: "16px", borderRadius: "2px",
            background: b.grade === null || b.grade === undefined
              ? "#e0e0e6" : (BAR_C[b.grade] || "#e0e0e6") } }),
          default: () => h("span", { style: MONO },
            (b.label || b.date) + " " + (barText[b.grade] || "没测")) })));
      const latencyText = (ms) => !ms ? "—"
        : ms >= 1000 ? (ms / 1000).toFixed(1) + "s" : ms + "ms";
      const cols = [
        { title: "模型", key: "model", minWidth: 240,
          render: (r) => h("span", { style: MONO }, r.model) },
        { title: "渠道", key: "channels", width: 180, render: (r) =>
            h(naive.NSpace, { size: 4, wrap: true }, () => (r.channels || []).length
              ? r.channels.map((c) => h(naive.NTag, { size: "small", bordered: false }, () => c))
              : [h("span", { style: MONO }, "—")]) },
        { title: "状态", key: "ok", width: 100, render: stTag },
        { title: "最近 24 小时", key: "heartbeats", minWidth: 190, render: barsCell },
        { title: "首字 TTFT", key: "ttft_ms", width: 110, render: (r) =>
            h("span", { style: MONO }, latencyText(r.ttft_ms)) },
        { title: "总耗时", key: "latency_ms", width: 100, render: (r) =>
            h("span", { style: MONO }, latencyText(r.latency_ms)) },
        { title: "上次检测", key: "checked_at", width: 140, render: (r) => r.checked_at
            ? h(naive.NTooltip, {}, {
                trigger: () => h("span", { style: MONO }, relTime(r.checked_at)),
                default: () => h("span", { style: MONO }, absTime(r.checked_at)) })
            : h("span", { style: MONO }, "—") },
        /* 失败原因:后端已脱敏(抹掉 key/token/上游地址),只在该模型整体失败时有值 */
        { title: "失败原因", key: "error", minWidth: 240, render: (r) => {
            if (!r.error) return h("span", { style: { color: "#9a9aa5" } }, "—");
            return h(naive.NTooltip, { style: "max-width:520px" }, {
              trigger: () => h("span", { style: Object.assign({ color: "#bf4038" }, MONO) },
                String(r.error).slice(0, 48)),
              default: () => h("span", { style: MONO }, r.error) });
          } },
      ];
      return { loading: s.loading, reload: s.reload, rows, view, filter, summary,
        interval, cols };
    },
    template: `
<Load :loading="loading">
<div class="console-page">
<n-card size="small" style="margin-bottom:20px">
  <n-space vertical :size="10">
    <n-space align="center" :size="18">
      <span>每小时自动检测一轮</span>
      <n-space align="center" :size="14">
        <span>正常 <n-text type="success" strong>{{ summary.ok || 0 }}</n-text></span>
        <span>偏慢 <n-text :type="summary.slow ? 'warning' : 'default'"
          strong>{{ summary.slow || 0 }}</n-text></span>
        <span>故障 <n-text :type="summary.fail ? 'error' : 'default'"
          strong>{{ summary.fail || 0 }}</n-text></span>
        <span>未检测 <n-text depth="2">{{ summary.pending || 0 }}</n-text></span>
      </n-space>
      <n-button secondary size="small" :loading="loading" @click="reload">刷新</n-button>
    </n-space>
    <n-text depth="3" style="font-size:12px">
      偏慢与故障的模型排在最前。每根状态条代表一次心跳(每小时巡检一轮,最近 24 个):
      红=不可用 黄=通过但首字超 10s 绿=正常 灰=该小时没测。
      多渠道故障切换时,该小时任一渠道能用就算绿。
      失败原因只保留可读部分,上游地址与凭据已隐去。
    </n-text>
  </n-space>
</n-card>
<n-card class="console-table-card" size="small" title="检测结果">
  <div class="console-toolbar">
    <n-input v-model:value="filter" clearable placeholder="搜索模型 / 渠道" style="width:280px;max-width:100%"/>
    <n-space align="center"><n-text depth="3">{{ view.length }} / {{ rows.length }} 个模型</n-text></n-space>
  </div>
  <n-data-table :columns="cols" :data="view" size="small" :bordered="false"
    :single-line="false" :scroll-x="1100" :row-key="(r) => r.model"/>
</n-card>
</div>
</Load>`,
  };

  window.BitPortalPages = { Load: Load, useFetch: useFetch,
    Dashboard: Dashboard, Keys: Keys, Usage: Usage, Wallet: Wallet,
    Billing: Billing, Invite: Invite, Share: Share, Leaderboard: Leaderboard,
    Profile: Profile, Plaza: Plaza, ModelStatus: ModelStatus,
    Playground: Playground };
})();
