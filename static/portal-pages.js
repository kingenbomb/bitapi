/* 控制台用户侧页面:概览 / 密钥 / 使用记录 / 钱包 / 计费 / 邀请 / 个人资料。
   数据全部来自 /api/*。 */
(function () {
  "use strict";
  const { h, ref, reactive, computed, onMounted, onUnmounted, watch } = Vue;
  const P = window.BitPortal;
  const { api, msg, dlg, copy, nf, usd, kf, fmt, MONO, SUBSTY, absTime, dayTime,
    relTime, st, POLICY, MODE, tag, money, reasonOf, ic, Ic, C, CT, tipRow, wrapTip,
    subline, stack, box, modelChip, costChip, timeRow, Pills, StatCard,
    BarChart, Donut, prov, provIcon, Avatar, shrinkAvatar,
    elapsedLv, frtLv, tpsOf, kindOf, normUsage, END_TEXT, EXPIRY_PRESETS,
    planDur, planKind, planLeft,
    CCSWITCH_APPS, ccServerAddress, buildCCSwitchURL, ccLoadModels, go,
    BASE, token, setToken, qs, download } = P;

  const dash = (v) => (v == null ? "—" : v);
  const pxCell = (v) => (v == null ? "—" : "$" + Number(v).toFixed(4));
  /* 对外的 API 端点就是本站自己。写死域名会让换了域名的部署复制出一个打不通的地址,
     所以和 ccServerAddress 一样从 location 算。 */
  const API_ENDPOINT = (location.origin + BASE).replace(/\/+$/, "") + "/";
  const MONO_ATTR = { style: MONO };

  /* 页面级加载/报错壳:一处写好,7 个页面不各写一遍 spin 和空态。 */
  const Load = {
    props: ["loading", "error", "onRetry"],
    render() {
      if (this.error)
        return h(naive.NResult, { status: "error", title: "加载失败",
          description: String(this.error) }, { footer: () =>
            h(naive.NButton, { onClick: this.onRetry }, () => "重试") });
      return h(naive.NSpin, { show: !!this.loading, style: "width:100%" },
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
    components: { Load, StatCard, Pills, BarChart, Donut, Ic },
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

      /* 八张卡按 4×2 排:第一行「钱与量」,第二行「Token 与性能」。
         性能指标合并成一张双行卡(RPM/TPM),不再单开一块面板。 */
      const cards = computed(() => {
        const list = [];
        if (policy.value === "balance")
          list.push({ label: "余额", value: "$" + fmt(o.value.balance),
            sub: "累计消费 $" + fmt(o.value.total_spent),
            color: "#15803d", icon: "wallet", tint: true });
        else
          list.push({ label: "计费策略", value: POLICY[policy.value] || policy.value,
            sub: o.value.group ? "分组 " + o.value.group : "未分配分组",
            color: "#3f6b8a", icon: "tag" });
        list.push({ label: "API 密钥", value: nf(keys.value.total),
          sub: keys.value.active + " 启用 · " +
            (keys.value.total - keys.value.active) + " 失效",
          color: "#4a6fa5", icon: "key" });
        list.push({ label: "今日请求", value: nf(today.value.requests || 0),
          sub: "累计 " + nf(total.value.requests || 0),
          color: "#3f6b8a", icon: "bars" });
        /* 原价与实扣并排:倍率不为 1 时两者不同,只显示一个会让人对不上账。 */
        list.push({ label: "今日消费",
          value: "$" + fmt(today.value.actual_cost || 0),
          value2: "$" + fmt(today.value.cost || 0),
          sub: "累计 $" + fmt(total.value.actual_cost || 0) +
            " / $" + fmt(total.value.cost || 0),
          color: "#6b5b95", icon: "dollar", tint: true });
        list.push({ label: "今日 Token", value: kf(tokOf(today.value)),
          sub: "输入 " + kf(today.value.input_tokens || 0) + " · 输出 " +
            kf(today.value.output_tokens || 0) + " · " + cacheNote(today.value),
          color: "#a17a10", icon: "cube" });
        list.push({ label: "累计 Token", value: kf(tokOf(total.value)),
          sub: "输入 " + kf(total.value.input_tokens || 0) + " · 输出 " +
            kf(total.value.output_tokens || 0) + " · " + cacheNote(total.value),
          color: "#6b5b95", icon: "coins" });
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

      /* ---- 图表 ---- */
      const series = computed(() => o.value.series || []);
      const metric = ref("tokens");
      const METRICS = [{ label: "Token", value: "tokens" },
        { label: "请求", value: "requests" }, { label: "消费", value: "cost" }];
      const METRIC_COLOR = { tokens: "#6b5b95", requests: "#3f6b8a",
        cost: "#a17a10" };
      const bars = computed(() => series.value.map((d) => {
        const v = d[metric.value] || 0;
        return { label: d.label, value: v,
          text: metric.value === "cost" ? "$" + fmt(v)
            : metric.value === "tokens" ? nf(v) + " tok" : nf(v) + " 次" };
      }));
      const barSum = computed(() => bars.value.reduce((a, x) => a + x.value, 0));
      const activeDays = computed(() =>
        bars.value.filter((x) => x.value > 0).length);

      /* 环形图用供应商色而不是一串固定色板:同一家的模型在别处也是这个颜色,
         图例和模型牌能对上。 */
      const pie = computed(() => {
        const rows = o.value.by_model || [];
        return rows.map((r) => ({ label: r.model, value: r.tokens || 0,
          text: nf(r.tokens || 0) + " tok",
          color: r.model.indexOf("其他") === 0 ? "#94a3b8" : prov(r.model).c,
          requests: r.requests || 0, cost: r.cost || 0 }));
      });
      const pieTotal = computed(() =>
        pie.value.reduce((a, x) => a + x.value, 0));

      /* 套餐卡要显示完整:分组、状态、计费、倍率、RPM、限额口径、
         可用模型、余额/累计消费。原先只有 4 行,可用模型与余额都看不到。 */
      const planRows = computed(() => {
        const rows = [["分组", o.value.group || "未分配"],
          ["计费方式", POLICY[policy.value] || policy.value]];
        if (o.value.rate_multiplier != null)
          rows.push(["倍率", "×" + o.value.rate_multiplier]);
        rows.push(["RPM 上限",
          o.value.rpm_limit ? o.value.rpm_limit + " 次/分" : "不限"]);
        rows.push(["限额口径",
          o.value.limit_unit === "tokens" ? "Token" : "请求次数"]);
        return rows;
      });
      const models = computed(() => o.value.supported_models || []);

      return Object.assign({ cards, byModel, cols, quota, o, policy, sum,
        checkin, checkingIn, doCheckin,
        series, metric, METRICS, METRIC_COLOR, bars, barSum, activeDays,
        pie, pieTotal, planRows, models, nf, kf, fmt, MONO,
        go: P.go }, s);
    },
    template: `
<Load :loading="loading" :error="error" :on-retry="reload">
<n-card size="small" style="margin-bottom:14px">
  <div style="display:flex;align-items:center;justify-content:space-between;
    gap:14px;flex-wrap:wrap">
    <n-space align="center" :wrap="false">
      <div class="bindicon" style="background:rgba(21,128,61,.10);color:#15803d">
        <Ic name="gift" :size="20"/>
      </div>
      <div>
        <div style="font-size:14px;font-weight:650">每日签到</div>
        <n-text depth="3" style="font-size:11.5px">
          <template v-if="checkin.checked_in">
            今日已领取 \${{ fmt(checkin.reward, 4) }}，明天再来
          </template>
          <template v-else>
            今日可随机领取 \${{ fmt(checkin.range && checkin.range.min, 4) }}
            ～ \${{ fmt(checkin.range && checkin.range.max, 4) }}
          </template>
        </n-text>
      </div>
    </n-space>
    <n-button type="primary" size="small" :loading="checkingIn"
      :disabled="!!checkin.checked_in" @click="doCheckin">
      {{ checkin.checked_in ? '今日已签到' : '立即签到' }}
    </n-button>
  </div>
</n-card>
<n-grid :cols="'1 500:2 900:4'" :x-gap="14" :y-gap="14" responsive="self">
  <n-gi v-for="c in cards" :key="c.label">
    <StatCard v-bind="c"/>
  </n-gi>
</n-grid>

<n-grid :cols="'1 1000:3'" :x-gap="16" :y-gap="16" responsive="self"
        style="margin-top:16px">
  <n-gi :span="2">
    <n-card size="small" title="近 14 天趋势">
      <template #header-extra>
        <n-space align="center" :size="10">
          <n-text depth="3" style="font-size:11.5px">
            {{ activeDays }}/14 天有调用
          </n-text>
          <n-radio-group v-model:value="metric" size="small">
            <n-radio-button v-for="m in METRICS" :key="m.value"
              :value="m.value">{{ m.label }}</n-radio-button>
          </n-radio-group>
        </n-space>
      </template>
      <BarChart :items="bars" :color="METRIC_COLOR[metric]" :height="132"/>
      <n-text depth="3" style="font-size:11.5px;display:block;margin-top:10px">
        合计
        <n-text :style="MONO">{{ metric === 'cost' ? '$' + fmt(barSum)
          : nf(barSum) }}</n-text>
        · 按本地日历日分桶,空白日表示当天没有调用(不是断线)。
      </n-text>
    </n-card>
  </n-gi>
  <n-gi>
    <n-card size="small" title="模型占比 · 累计">
      <div v-if="pieTotal" style="display:flex;align-items:center;gap:16px">
        <Donut :items="pie" :center="kf(pieTotal)" sub="Tokens"/>
        <n-space vertical :size="6" style="flex:1;min-width:0">
          <n-tooltip v-for="p in pie" :key="p.label" placement="left">
            <template #trigger>
              <div class="legrow">
                <span class="legdot" :style="{background:p.color}"></span>
                <span style="flex:1;min-width:0;overflow:hidden;
                  text-overflow:ellipsis;white-space:nowrap">{{ p.label }}</span>
                <span :style="MONO" style="opacity:.6">
                  {{ Math.round(p.value / pieTotal * 100) }}%</span>
              </div>
            </template>
            <div style="font-size:11.5px">
              <div :style="MONO">{{ p.label }}</div>
              <div style="opacity:.75;margin-top:3px">
                {{ nf(p.value) }} tok · {{ nf(p.requests) }} 次 ·
                \${{ fmt(p.cost) }}
              </div>
            </div>
          </n-tooltip>
        </n-space>
      </div>
      <n-empty v-else description="还没有调用记录" size="small"
        style="padding:20px 0"/>
    </n-card>
  </n-gi>
</n-grid>

<n-grid :cols="'1 1000:3'" :x-gap="16" :y-gap="16" responsive="self"
        style="margin-top:16px">
  <n-gi :span="2">
    <n-card title="今日模型分布" size="small">
      <n-data-table :columns="cols" :data="byModel" :bordered="false" size="small"/>
      <template #footer v-if="!byModel.length">
        <n-text depth="3" style="font-size:12px">今日还没有调用记录。</n-text>
      </template>
    </n-card>
  </n-gi>
  <n-gi>
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
            <span :style="r[0] === '倍率' || r[0] === 'RPM 上限' ? MONO : null">
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
            可用模型 · {{ models.length }} 项
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
  </n-gi>
</n-grid>
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
<n-grid :cols="'1 560:2 1000:4'" :x-gap="14" :y-gap="14" responsive="self"
  style="margin-bottom:14px">
  <n-gi v-for="c in cards" :key="c.label">
    <StatCard :label="c.label" :value="c.value" :sub="c.sub" :color="c.color"
      :icon="c.icon" :tint="c.tint"/>
  </n-gi>
</n-grid>

<n-card size="small" style="margin-bottom:14px" content-style="padding:14px 15px">
  <n-space align="center" :size="10" style="row-gap:10px">
    <n-input v-model:value="q" clearable placeholder="搜索名称或密钥尾号…"
      style="width:250px">
      <template #prefix><Ic name="search" :size="14"/></template>
    </n-input>
    <n-select v-model:value="fstatus" :options="statusOpts" style="width:148px"/>
    <n-text depth="3" style="font-size:11.5px">API 端点</n-text>
    <n-tag size="small" round :bordered="false" :style="MONO">
      {{ endpoint }}</n-tag>
    <n-button size="small" secondary aria-label="复制 API 端点"
      @click="copyEndpoint">
      <template #icon><Ic name="copy" :size="14"/></template>
      复制
    </n-button>
    <div style="flex:1"></div>
    <n-button size="small" secondary @click="reload">刷新</n-button>
    <n-button type="primary" size="small" @click="openCreate">
      <template #icon><Ic name="key" :size="15"/></template>
      创建密钥</n-button>
  </n-space>
</n-card>

<n-card size="small" content-style="padding:0">
  <Load :loading="loading" :error="error" :on-retry="reload">
    <n-data-table :columns="cols" :data="view" :bordered="false" size="small"
      :scroll-x="1112" :row-key="(r) => r.id" :pagination="pagination"/>
  </Load>
</n-card>
<n-text depth="3" style="font-size:11.5px;display:block;margin:10px 2px 0">
  密钥只显示首尾片段,点复制按钮取完整明文(避免截图与投屏时整表泄露)。
  分组每行相同 —— 密钥不能改分组;「可用模型」为空表示跟随分组白名单,
  鼠标悬停可看分组允许的全部模型。
</n-text>
<n-drawer v-model:show="dr.show" :width="520" placement="right">
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
    components: { Load, Pills, Ic, StatCard },
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
         用户会先怀疑账。时间上自选区间优先,没选就按上面的今日/7 天/30 天。 */
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
          { label: "总消费", value: "$" + (a.actual_cost || 0).toFixed(4),
            sub: "长上下文 " + (a.long || 0) + " 次 · 失败 " + (a.failed || 0) + " 次",
            color: "#15803d", icon: "dollar", tint: true },
          { label: "平均耗时", value: ((a.avg_ms || 0) / 1000).toFixed(2) + "s",
            sub: a.frt_ms == null ? "无首字记录"
              : "首字均 " + (a.frt_ms / 1000).toFixed(2) + "s",
            color: "#6b5b95", icon: "clock" },
        ];
      });
      const stats = computed(() => [
        ["用量", usd(totals.value.actual_cost || 0), C.cost],
        ["请求", nf(totals.value.requests || 0), C.ctx],
        ["失败", nf(totals.value.failed || 0),
          totals.value.failed ? C.err : "#94a3b8"],
      ]);
      const cols = [
        { title: "时间", key: "t", width: 152, render: (r) => {
            const k = kindOf(r);
            return stack(
              h("span", { style: Object.assign({ fontSize: "12px" }, MONO) },
                absTime(r.t)),
              h("span", { style: { fontSize: "11px", color: k[1],
                opacity: k[1] ? 0.9 : 0.5 } }, k[0])); } },
        { title: "密钥", key: "key", width: 148, render: (r) => stack(
            box([ic("key", 12, undefined, 2),
                 h("span", { style: { whiteSpace: "nowrap" } }, r.key)],
                { fontWeight: 550 }),
            r.ratio != null && r.ratio !== 1 ? "倍率 " + r.ratio + "x" : null) },
        { title: "模型", key: "model", width: 190,
          render: (r) => modelChip(r.model) },
        { title: "流", key: "stream", width: 72, render: (r) => {
            const sp = tpsOf(r);
            return stack(
              h("span", { style: { fontSize: "12px", fontWeight: 600,
                color: r.stream ? C.ctx : undefined,
                opacity: r.stream ? 1 : 0.5 } }, r.stream ? "流" : "非流"),
              sp ? h("span", { style: Object.assign({ fontSize: "11px",
                opacity: 0.5 }, MONO) }, sp + " t/s") : null); } },
        /* 缓存读写没有单独落列,只能显示由判定量反推出的合计。 */
        { title: "Tokens", key: "i", width: 156, render: (r) => {
            const tot = r.i + r.o + (r.cache || 0);
            const cache = r.cache ? h("span", { style: Object.assign(
              { fontSize: "10.5px", opacity: 0.5, whiteSpace: "nowrap" }, MONO) },
              "缓存 " + nf(r.cache)) : null;
            return wrapTip(h("div", { style: { display: "flex",
              flexDirection: "column", alignItems: "flex-start", gap: "2px",
              cursor: "help" } }, [
              h("span", { style: Object.assign({ fontSize: "12px",
                fontWeight: 600, whiteSpace: "nowrap" }, MONO) },
                nf(r.i) + " / " + nf(r.o)),
              cache,
            ]), [
              tipRow("输入", nf(r.i), CT.in),
              tipRow("输出", nf(r.o), CT.out),
              r.cache ? tipRow("缓存合计", nf(r.cache), CT.cr) : null,
              r.ctx == null ? null
                : tipRow("判定 ctx", nf(r.ctx) + (r.thr ? " / " + nf(r.thr) : ""),
                  CT.ctx),
              tipRow("合计", nf(tot), undefined, true),
            ]); } },
        { title: "费用", key: "cost", width: 112, render: (r) => {
            const chip = r.free ? costChip("免费", "#15803d")
              : costChip(usd(r.cost).slice(1));
            const rows = [];
            if (r.px) {
              rows.push(tipRow("输入价", pxCell(r.px.i) + " /M", CT.in));
              rows.push(tipRow("输出价", pxCell(r.px.o) + " /M", CT.out));
              if (r.px.cr != null)
                rows.push(tipRow("缓存读价", pxCell(r.px.cr) + " /M", CT.cr));
              if (r.px.cw != null)
                rows.push(tipRow("缓存写价", pxCell(r.px.cw) + " /M", CT.cw));
              rows.push(tipRow("档位", r.tier === "long" ? "长上下文" : "标准",
                r.tier === "long" ? CT.cw : undefined));
            } else if (r.perReq) {
              rows.push(tipRow("按次单价", pxCell(r.perPrice), CT.cost));
              rows.push(tipRow("次数", nf(r.units || 1)));
            } else {
              rows.push(tipRow("计费", "免费策略 · 无单价快照", CT.in));
            }
            if (r.ratio != null) rows.push(tipRow("分组倍率", r.ratio + "x"));
            rows.push(tipRow("实际扣费", usd(r.cost), CT.cost, true));
            return wrapTip(chip, rows); } },
        { title: "耗时", key: "ms", width: 116, render: (r) => {
            return h("div", { style: { display: "flex", flexDirection: "column",
              gap: "2px" } }, [
              timeRow("首字", r.frt == null ? "—"
                : (r.frt / 1000).toFixed(1) + "s",
                r.frt == null ? null : frtLv(r.frt / 1000)),
              timeRow("耗时", (r.ms / 1000).toFixed(1) + "s",
                elapsedLv(r.ms / 1000)),
            ]); } },
        { title: "详情", key: "rid", minWidth: 186, render: (r) => {
            const long = r.tier === "long";
            const txt = r.free ? "免费策略 · 不计费"
              : r.perReq ? "按次 · " + pxCell(r.perPrice) + "/次"
              : r.px ? (long ? "长档" : "标准") + " · " + pxCell(r.px.i) +
                  " / " + pxCell(r.px.o) + "/M"
              : "无定价快照";
            return h("div", { style: { display: "flex", alignItems: "center",
              gap: "5px" } }, [
              !r.ok ? h(naive.NTooltip, { placement: "left" }, {
                trigger: () => h("span", { style: { display: "flex",
                  cursor: "help", color: C.err } }, ic("alert", 13)),
                default: () => END_TEXT[r.end] || r.end }) : null,
              long ? ic("fire", 12, C.cw) : null,
              h("span", { class: "dtl", style: Object.assign({ fontSize: "11.5px",
                whiteSpace: "nowrap", color: long ? C.cw : undefined,
                opacity: long ? 1 : 0.72 }, MONO),
                onClick: () => { detail.value = r; } }, txt),
            ]); } },
      ];
      const rowClass = (r) => (!r.ok ? "row-err" : "");
      return Object.assign({ rows, cols, rowClass, stats, cards, detail,
        range, f, modelOpts, keyOpts, reset, query, nf, usd, kf, fmt, MONO,
        tpsOf, C, dash, pxCell, absTime, END_TEXT, MODE, POLICY,
        total, page, pageSize, jump, resize, exportCsv, exporting,
        copyRid: () => detail.value && detail.value.rid
          ? copy(detail.value.rid, "已复制请求 ID") : msg.warning("该记录没有请求 ID"),
      }, s);
    },
    template: `
<n-grid :cols="'1 640:2 1180:4'" :x-gap="12" :y-gap="12" responsive="self"
        style="margin-bottom:14px">
  <n-gi v-for="c in cards" :key="c.label">
    <StatCard :label="c.label" :value="c.value" :sub="c.sub" :color="c.color"
      :icon="c.icon" :tint="c.tint"/>
  </n-gi>
</n-grid>

<n-card size="small" title="通用日志" :segmented="{content:true}"
        :content-style="'padding:14px 15px'">
  <n-grid :cols="'1 700:2 1100:5'" :x-gap="8" :y-gap="8" responsive="self">
    <n-gi :span="2"><n-date-picker v-model:value="f.span" type="datetimerange"
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
    <Pills :items="stats"/>
    <n-space :size="7" align="center">
      <n-radio-group v-model:value="range" size="small" :disabled="!!f.span">
        <n-radio-button value="day">今日</n-radio-button>
        <n-radio-button value="week">7 天</n-radio-button>
        <n-radio-button value="month">30 天</n-radio-button>
      </n-radio-group>
      <n-button size="small" secondary @click="reset">重置</n-button>
      <n-button size="small" secondary :loading="exporting" @click="exportCsv">
        导出 CSV</n-button>
      <n-button size="small" type="primary" :loading="loading" @click="query">
        查询</n-button>
    </n-space>
  </n-space>

  <template #footer>
    <Load :loading="loading" :error="error" :on-retry="reload">
      <n-data-table :columns="cols" :data="rows" :bordered="false" size="small"
        :row-class-name="rowClass" :scroll-x="1132" :row-key="(r) => r.id"/>
      <n-space justify="space-between" align="center" style="margin-top:10px" wrap>
        <n-text depth="3" style="font-size:11.5px">
          自选时间区间优先于右上的今日 / 7 天 / 30 天;统计卡与列表始终是同一组条件。
        </n-text>
        <n-pagination :page="page" :page-size="pageSize" :item-count="total"
          :page-slot="6" show-size-picker :page-sizes="[20,50,100,200]"
          @update:page="jump" @update:page-size="resize">
          <template #prefix>共 {{ nf(total) }} 条</template>
        </n-pagination>
      </n-space>
    </Load>
  </template>
</n-card>

<n-drawer :show="!!detail" :width="470" placement="right"
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
          <n-descriptions-item label="缓存 token 合计">
            <span :style="MONO" style="color:#3f6b8a;font-weight:650">
              {{ detail.cache == null ? '—' : nf(detail.cache) }}</span>
          </n-descriptions-item>
          <n-descriptions-item label="判定量 ctx">
            <span :style="MONO" style="color:#4a6fa5;font-weight:650">
              {{ detail.ctx == null ? '—' : nf(detail.ctx) }}</span>
            <n-text depth="3" v-if="detail.thr" style="font-size:11px">
              &nbsp;/ {{ nf(detail.thr) }}</n-text>
          </n-descriptions-item>
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
        <n-text depth="3" v-if="detail.cache != null"
          style="font-size:11px;display:block;margin-top:5px">
          日志未分列缓存读/写,此处由「判定量 ctx − 输入」反推合计。
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
      const provider = ref("mock");
      const minTopup = ref(1);
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
        if (r[2].min_topup) {
          minTopup.value = r[2].min_topup;
          if (amount.value < r[2].min_topup) amount.value = r[2].min_topup;
        }
        if (providers.value.length &&
            !providers.value.some((p) => p.value === provider.value))
          provider.value = providers.value[0].value;
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
        { title: "渠道", key: "provider", width: 104 },
        { title: "状态", key: "status", width: 96, render: (r) => {
            const m = OST[r.status] || ["default", r.status];
            return tag(m[0], m[1]); } },
        { title: "创建时间", key: "created_at", width: 150, render: (r) =>
            h("span", { style: Object.assign({ fontSize: "12px" }, MONO) },
              absTime(r.created_at)) },
      ];
      return Object.assign({ bal, entries, orders, providers, inv, amount,
        provider, code, busy, lcols, ocols, pay, doRedeem, fmt, usd,
        minTopup, watching }, s);
    },
    template: `
<Load :loading="loading" :error="error" :on-retry="reload">
<n-grid :cols="'1 640:3'" :x-gap="12" :y-gap="12" responsive="self">
  <n-gi><StatCard label="可用余额" :value="'$' + fmt(bal.balance, 2)"
    sub="消费按次实时扣减" color="#a8613c" icon="wallet" :tint="true"/></n-gi>
  <!-- 消费额不锁 2 位:小额消费在两位小数下会显示成 $0.00,与概览页对不上。 -->
  <n-gi><StatCard label="累计消费" :value="'$' + fmt(bal.total_spent)"
    sub="请求扣费与买卡合计" color="#6b5b95" icon="dollar"/></n-gi>
  <n-gi><StatCard label="累计返佣" :value="'$' + fmt(inv.earned, 2)"
    :sub="'已邀请 ' + (inv.invitees || 0) + ' 人'" color="#a06a2c" icon="gift"/></n-gi>
</n-grid>

<n-grid :cols="'1 900:2'" :x-gap="14" :y-gap="14" responsive="self"
        style="margin-top:14px">
  <n-gi><n-card size="small" title="充值">
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
        placeholder="支付渠道"/>
      <n-alert v-if="!providers.length" type="warning" :bordered="false"
        :show-icon="false" style="font-size:12.5px">
        站点未启用任何支付渠道,请联系管理员配置 BITAPI_PAYMENT_PROVIDERS。</n-alert>
      <n-alert v-if="watching" type="info" :bordered="false"
        :show-icon="false" style="font-size:12.5px">
        <n-space align="center" :size="8">
          <n-spin :size="14"/>
          <span>等待 {{ watching.out_trade_no }} 到账,付款完成后这里会自动更新。</span>
        </n-space>
      </n-alert>
      <n-button type="primary" block :loading="busy.pay"
        :disabled="!providers.length" @click="pay">
        去支付 \${{ fmt(amount, 2) }}</n-button>
    </n-space>
  </n-card></n-gi>
  <n-gi><n-card size="small" title="兑换码">
    <n-space vertical :size="14">
      <n-input v-model:value="code" placeholder="XXXX-XXXX-XXXX-XXXX"
        @keyup.enter="doRedeem"/>
      <n-alert type="default" :bordered="false" style="font-size:12.5px">
        兑换后额度立即计入余额,可在下方流水按来源筛选。充值到账同样走内部兑换码,
        因此充值流水的来源显示为「充值到账」。</n-alert>
      <n-button block :loading="busy.redeem" @click="doRedeem">兑换</n-button>
    </n-space>
  </n-card></n-gi>
</n-grid>

<n-card size="small" title="余额流水" style="margin-top:14px">
  <n-data-table :columns="lcols" :data="entries" :bordered="false" size="small"
    :scroll-x="792" :row-key="(r) => r.id"
    :pagination="{pageSize:10,pageSlot:5,prefix:({itemCount})=>'总计 '+itemCount}"/>
</n-card>
<n-card size="small" title="我的订单" style="margin-top:14px">
  <n-data-table :columns="ocols" :data="orders" :bordered="false" size="small"
    :scroll-x="636" :row-key="(r) => r.id"
    :pagination="{pageSize:10,pageSlot:5,prefix:({itemCount})=>'总计 '+itemCount}"/>
</n-card>
</Load>`,
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
            h(naive.NText, { depth: 3, style: SUBSTY }, () => r.idem_key || "—") },
      ];
      return Object.assign({ inv, affRows, cols, link, fmt,
        copyCode: () => copy(inv.value.aff_code || "", "已复制返佣码"),
        copyLink: () => copy(link.value, "已复制邀请链接") }, s);
    },
    template: `
<Load :loading="loading" :error="error" :on-retry="reload">
<n-grid :cols="'1 900:2'" :x-gap="14" :y-gap="14" responsive="self">
  <n-gi><n-card size="small" title="我的返佣码">
    <n-space vertical :size="14">
      <n-input-group>
        <n-input :value="inv.aff_code || ''" readonly/>
        <n-button type="primary" ghost @click="copyCode">复制</n-button>
      </n-input-group>
      <n-input-group>
        <n-input :value="link" readonly/>
        <n-button ghost @click="copyLink">复制链接</n-button>
      </n-input-group>
      <n-text depth="3" style="font-size:12.5px;line-height:1.6">
        返佣规则由站点启用的插件决定,不同站点比例不同,以下方实际到账流水为准。
        开放注册时邀请人一次绑定后终身有效；站点开启邀请制后,本码不能替代
        管理端发放的一次性注册邀请码。</n-text>
    </n-space>
  </n-card></n-gi>
  <n-gi><n-grid :cols="2" :x-gap="12" :y-gap="12">
    <n-gi><StatCard label="已邀请" :value="String(inv.invitees || 0)"
      sub="成功注册人数" color="#3f6b8a" icon="user"/></n-gi>
    <n-gi><StatCard label="返佣总额" :value="'$' + fmt(inv.earned, 2)"
      sub="已计入可用余额" color="#a06a2c" icon="gift" :tint="true"/></n-gi>
    <n-gi :span="2"><n-alert type="default" :bordered="false"
      :show-icon="false" style="font-size:12.5px">
      接口只提供邀请人数与返佣总额,不返回被邀请人名单,因此这里不展示被邀请人明细,
      改为列出每一笔返佣入账。</n-alert></n-gi>
  </n-grid></n-gi>
</n-grid>

<n-card size="small" title="返佣入账" style="margin-top:14px">
  <n-data-table :columns="cols" :data="affRows" :bordered="false" size="small"
    :scroll-x="470" :row-key="(r) => r.id"
    :pagination="{pageSize:10,pageSlot:5,prefix:({itemCount})=>'总计 '+itemCount}"/>
</n-card>
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
         rpm_limit 已随 /me 一起返回,不必再要一次 /billing。 */
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
        dropAvatar, copy, tf, totpSetup, totpEnable, totpDisable }, s);
    },
    template: `
<Load :loading="loading" :error="error" :on-retry="reload">
<n-card size="small" class="hero" style="margin-bottom:14px"
  content-style="padding:20px 22px">
  <div class="herorow">
    <Avatar :src="me.avatar" :text="initial" :size="72" radius="18px"/>
    <div style="flex:1;min-width:0">
      <n-space align="center" :size="8" style="margin-bottom:3px">
        <span class="heroname">{{ shown }}</span>
        <n-tag size="small" round :bordered="false"
          :type="me.role === 'admin' ? 'warning' : 'default'">
          {{ me.role === 'admin' ? '管理员' : '用户' }}</n-tag>
        <n-tag size="small" round :bordered="false"
          :type="me.status === 'active' ? 'success' : 'error'">
          {{ me.status === 'active' ? '启用' : '已禁用' }}</n-tag>
      </n-space>
      <div class="herosub">{{ me.email }}</div>
    </div>
  </div>
  <n-grid :cols="'1 640:2 900:4'" :x-gap="12" :y-gap="12" responsive="self"
    style="margin-top:16px">
    <n-gi><div class="herocell">
      <div class="herok">账户余额</div>
      <div class="herov" :style="MONO">\${{ fmt(me.balance) }}</div>
    </div></n-gi>
    <n-gi><div class="herocell">
      <div class="herok">累计消费</div>
      <div class="herov" :style="MONO">\${{ fmt(me.total_spent) }}</div>
    </div></n-gi>
    <n-gi><div class="herocell">
      <div class="herok">速率上限</div>
      <div class="herov" :style="MONO">
        {{ me.rpm_limit ? me.rpm_limit : '不限'
        }}<span v-if="me.rpm_limit" class="herou"> 次/分</span></div>
    </div></n-gi>
    <n-gi><div class="herocell">
      <div class="herok">注册时间</div>
      <div class="herov" style="font-size:17px">{{ regMonth }}</div>
    </div></n-gi>
  </n-grid>
</n-card>

<n-card size="small" style="margin-bottom:14px">
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

<n-card size="small" style="margin-bottom:14px">
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

<n-card v-if="me.has_password" size="small">
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

<n-card v-if="me.has_password" size="small" style="margin-top:14px">
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
        rows.push(["速率", pl.rpm_limit ? pl.rpm_limit + " RPM" : "不限 RPM"]);
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
<n-alert v-if="!b.group" type="warning" :bordered="false"
  style="font-size:12.5px;margin-bottom:14px">
  这个账号还没有绑定计费分组,现在调用会被拒绝。买一张下面的套餐可以立刻用起来,
  也可以联系管理员分配默认分组。
</n-alert>

<n-grid :cols="'1 700:3'" :x-gap="14" :y-gap="14" responsive="self"
  style="margin-bottom:14px">
  <n-gi><StatCard label="可用余额" :value="'$' + fmt(bal)"
    :sub="bal > 0 ? '买套餐与按量扣费都从这里出' : '余额不足将拒绝请求'"
    color="#a8613c" icon="wallet" tint/></n-gi>
  <n-gi><StatCard label="累计消费" :value="'$' + fmt(b.total_spent)"
    sub="请求扣费与买卡合计" color="#6b5b95" icon="dollar"/></n-gi>
  <n-gi><StatCard :label="onPlan ? '套餐剩余' : '当前计费'"
    :value="onPlan ? left : '按量计费'"
    :sub="onPlan
      ? cur.name + (cur.expires_at ? ' · ' + absTime(cur.expires_at) + ' 到期' : '')
      : (p.base_group ? '默认分组 ' + p.base_group : '未分配分组')"
    color="#3f6b8a" icon="tag"/></n-gi>
</n-grid>

<n-card size="small" title="订阅套餐" style="margin-bottom:14px">
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
        <n-descriptions-item label="RPM 上限">
          <span :style="MONO">{{ b.rpm_limit || '不限' }}</span>
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

  <n-card v-if="policy === 'balance'" size="small"
    title="生效单价 · 美元 / 1M token" style="margin-top:14px">
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
      return "$" + fmt(r.input_price) + " 入 · $" + fmt(r.output_price) + " 出";
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
      ];
      const rowProps = (r) => ({ style: "cursor:pointer",
        onClick: () => { detail.value = r; } });

      return Object.assign({ d, q, fv, fm, view, sort, detail, list, reset,
        vendorOpts, modeOpts, priceRows, callable, cols, rowProps,
        renderOpt, MODE, MODE_TAG, SRC_TEXT, SORT_OPTS, MONO, SUBSTY, fmt, copy,
        priceBrief, total: computed(() => rows.value.length) }, s);
    },
    template: `
<Load :loading="loading" :error="error" :on-retry="reload">
<n-card size="small" :bordered="false" style="margin-bottom:14px"
  content-style="padding:12px 14px">
  <n-space align="center" :size="10" style="row-gap:10px">
    <n-input v-model:value="q" clearable placeholder="搜模型 / 供应商"
      style="width:224px"/>
    <n-select v-model:value="fv" :options="vendorOpts" clearable
      placeholder="供应商" style="width:168px" :render-label="renderOpt"/>
    <n-select v-model:value="fm" :options="modeOpts" clearable
      placeholder="计费方式" style="width:150px"/>
    <n-select v-model:value="sort" :options="SORT_OPTS" style="width:132px"/>
    <n-radio-group v-model:value="view" size="small">
      <n-radio-button value="card">卡片</n-radio-button>
      <n-radio-button value="table">表格</n-radio-button>
    </n-radio-group>
    <n-button v-if="q || fv || fm || sort !== 'name'" quaternary
      size="small" @click="reset">清空</n-button>
    <n-text depth="3" style="font-size:11.5px">
      {{ list.length }} / {{ total }} 个模型 ·
      单价为原价,结算再乘分组倍率 ×{{ d.rate_multiplier }}
    </n-text>
  </n-space>
  <!-- 游客看的是默认分组的价与可见范围。不写出来的话,注册后落到别的组、
       价格对不上,只会被当成标错价。 -->
  <n-alert v-if="d.guest" type="default" :bordered="false" :show-icon="false"
    style="margin-top:10px;font-size:12px">
    未登录,按默认分组「{{ d.group || '—' }}」的价格与可见范围显示。
    注册后如果被分到别的分组,单价与可用模型会跟着变。
  </n-alert>
</n-card>

<n-empty v-if="!list.length" description="没有匹配的模型"
  style="padding:52px 0"/>
<n-grid v-else-if="view === 'card'" :cols="'1 620:2 940:3'" :x-gap="12"
  :y-gap="12" responsive="self">
  <n-gi v-for="r in list" :key="r.model">
    <n-card size="small" hoverable style="cursor:pointer;height:100%"
      content-style="padding:13px 14px" @click="detail = r">
      <div style="display:flex;align-items:center;gap:8px">
        <PIcon :model="r.model" :size="20"/>
        <div style="min-width:0;flex:1">
          <div :style="[MONO, {fontSize:'13px',fontWeight:650,
            whiteSpace:'nowrap',overflow:'hidden',textOverflow:'ellipsis'}]"
            :title="r.model">{{ r.model }}</div>
          <div :style="SUBSTY" style="opacity:.6;margin-top:2px">
            {{ r.p.n }}</div>
        </div>
        <n-button quaternary circle size="tiny"
          @click.stop="copy(r.variants.length ? r.variants[0].model : r.model,
            '已复制模型名')">
          <Ic name="copy" :size="14"/></n-button>
      </div>
      <div style="display:flex;align-items:center;gap:7px;margin-top:11px">
        <n-tag size="small" round :bordered="false"
          :type="MODE_TAG[r.billing_mode] || 'default'">
          {{ MODE[r.billing_mode] || '混合计费' }}</n-tag>
        <span v-if="r.billing_mode !== 'free'"
          :style="[MONO, {fontSize:'12.5px',fontWeight:600}]">
          {{ priceBrief(r) }}</span>
        <n-text v-else-if="r.price_source === 'fallback'" depth="3"
          style="font-size:11.5px">未配单价</n-text>
      </div>
      <div v-if="r.variants.length" :style="SUBSTY"
        style="opacity:.55;margin-top:7px">{{ r.variants.length }} 种规格可选</div>
    </n-card>
  </n-gi>
</n-grid>

<n-data-table v-else :columns="cols" :data="list" :bordered="false" size="small"
  :single-line="false" :scroll-x="830" :row-key="(r) => r.model"
  :row-props="rowProps"
  :pagination="{pageSize:20,pageSlot:5,showSizePicker:true,
    pageSizes:[20,50,100],prefix:({itemCount})=>'总计 '+itemCount}"/>

<n-drawer :show="!!detail" :width="500" placement="right"
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
        /* 只留对话模型:chat 位由渠道的 CAP_CHAT 声明,图/视频渠道进不来。 */
        models.value = (r.data || []).filter((m) => m.chat);
        if (!model.value && models.value.length)
          model.value = models.value[0].model;
      }));

      const turns = ref([]);      // {role, text, reasoning, frt, ms, usage, err}
      const draft = ref("");
      const busy = ref(false);
      let abort = null;

      const opts = computed(() => models.value.map((m) => ({
        value: m.model, label: m.model, icon: prov(m.model),
        channel: m.channel })));
      const renderOpt = (o) => (o && o.icon
        ? h("div", { style: { display: "flex", alignItems: "center",
            gap: "7px" } }, [provIcon(o.icon, 15),
            h("span", null, o.label),
            h("span", { style: { opacity: 0.45, fontSize: "11.5px" } },
              o.channel)])
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
                       Authorization: "Bearer " + token() },
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
        busy, send, stop, clear, MONO, SUBSTY, kf, nf }, s);
    },
    template: `
<Load :loading="loading" :error="error" :on-retry="reload">
<n-card size="small" :bordered="false" style="margin-bottom:12px"
  content-style="padding:11px 14px">
  <n-space align="center" :size="10">
    <n-select v-model:value="model" :options="opts" filterable
      :disabled="busy" placeholder="选择模型" style="width:300px"
      :render-label="renderOpt"/>
    <n-button size="small" quaternary :disabled="busy || !turns.length"
      @click="clear">清空对话</n-button>
    <n-text depth="3" style="font-size:11.5px">
      体验就是一次真实请求,按你当前分组的单价扣费,消费会进「使用记录」
    </n-text>
  </n-space>
  <n-alert v-if="!models.length" type="warning" :bordered="false"
    :show-icon="false" style="margin-top:10px;font-size:12px">
    当前分组没有可用的对话模型。图片和视频模型走生成接口,不在这里体验。
  </n-alert>
</n-card>

<n-empty v-if="!turns.length" description="发一句试试" style="padding:64px 0"/>
<n-space v-else vertical :size="12" style="margin-bottom:14px">
  <div v-for="(t, i) in turns" :key="i"
    :style="{display:'flex', justifyContent: t.role === 'user'
      ? 'flex-end' : 'flex-start'}">
    <div :style="{maxWidth:'760px', minWidth:0}">
      <n-card size="small" :bordered="false"
        :content-style="'padding:11px 13px'"
        :style="{background: t.role === 'user'
          ? 'rgba(168,97,60,.09)' : 'rgba(127,127,127,.07)'}">
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

<n-card size="small" :bordered="false" content-style="padding:11px 12px">
  <n-space vertical :size="8">
    <n-input v-model:value="draft" type="textarea" :rows="3"
      placeholder="说点什么… Ctrl/⌘ + Enter 发送"
      @keydown.ctrl.enter="send" @keydown.meta.enter="send"/>
    <n-space justify="end" :size="8">
      <n-button v-if="busy" size="small" @click="stop">停止</n-button>
      <n-button size="small" type="primary" :disabled="busy || !draft.trim()"
        :loading="busy" @click="send">发送</n-button>
    </n-space>
  </n-space>
</n-card>
</Load>`,
  };

  window.BitPortalPages = { Load: Load, useFetch: useFetch,
    Dashboard: Dashboard, Keys: Keys, Usage: Usage, Wallet: Wallet,
    Billing: Billing, Invite: Invite, Profile: Profile, Plaza: Plaza,
    Playground: Playground };
})();
