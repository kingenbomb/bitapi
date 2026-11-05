/* bit-api 控制台共用层:主题、请求、格式化、图标与小组件。 */
(function () {
  "use strict";
  const { h, ref, computed, watch, watchEffect, onMounted, onUnmounted } = Vue;

  /* API 基址。原先无条件 replace,只在 /portal 结尾时才对 —— 反代把 /login、
     /register、/ 也指到这份 HTML 之后,pathname 是 /login,replace 没匹配上,
     BASE 就成了 "/login",于是登录请求打到 /login/api/login → Not Found。
     只有 pathname 真的以 /portal 结尾时才剥离(那是子路径反代的情形),
     否则一律按根路径算。 */
  const BASE = /\/portal\/?$/.test(location.pathname)
    ? location.pathname.replace(/\/portal\/?$/, "")
    : "";
  const TOKEN_KEY = "bitapi_token";
  const THEME_KEY = "bitapi_theme";

  localStorage.removeItem(TOKEN_KEY);
  let sessionCleared = false;
  const csrf = () => (document.cookie.split("; ").find((v) => v.startsWith("bitapi_csrf=")) || "").split("=").slice(1).join("=");
  const token = () => !sessionCleared && csrf() ? "cookie" : "";
  const setToken = () => { sessionCleared = false; };
  const authHeaders = () => ({ "X-Bitapi-Session": "cookie", "X-CSRF-Token": csrf() });
  const clearToken = () => {
    const headers = authHeaders();
    sessionCleared = true;
    return fetch(BASE + "/api/logout", { method: "POST", headers, credentials: "same-origin" }).catch(() => {});
  };

  /* 401 一律视为登录态失效:清 token 并回登录页,避免各页面各写一遍。 */
  function api(method, path, body, { timeout = 0 } = {}) {
    const opts = { method: method, headers: authHeaders(), credentials: "same-origin" };
    const controller = timeout ? new AbortController() : null;
    let expired = false;
    const timeoutError = () => new Error("测试请求等待超时，请稍后重试；可在测试记录中查看结果");
    const timer = controller ? setTimeout(() => { expired = true; controller.abort(); }, timeout) : null;
    if (controller) opts.signal = controller.signal;
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    return fetch(BASE + "/api" + path, opts).then((r) =>
      r.json().catch(() => { if (expired) throw timeoutError(); return {}; }).then((j) => {
        if (!r.ok) {
          const d = j && j.detail;
          const msg = d && typeof d === "object"
            ? d.message || JSON.stringify(d) : d || r.status;
          if (r.status === 401 && token()) {
            clearToken();
            location.hash = "#/login";
          }
          throw new Error(msg);
        }
        if (j.authenticated) sessionCleared = false;
        return j;
      })).catch((error) => { if (expired) throw timeoutError(); throw error; })
      .finally(() => { if (timer !== null) clearTimeout(timer); });
  }

  /* 查询串:跳过 null / undefined / "" / "all",数组按逗号拼。
     筛选表单里「没选」有四种写法,让每个调用点自己判会各判一套。 */
  function qs(params) {
    const p = new URLSearchParams();
    Object.keys(params || {}).forEach((k) => {
      const v = params[k];
      if (v === null || v === undefined || v === "" || v === "all") return;
      p.set(k, Array.isArray(v) ? v.join(",") : String(v));
    });
    const s = p.toString();
    return s ? "?" + s : "";
  }

  /* 带 JWT 的文件下载:<a href> 不会带 Authorization 头,导出 CSV 只能先 fetch
     成 blob 再造一个临时链接点掉。文件名优先用服务端 Content-Disposition 里的。 */
  function download(path, fallbackName) {
    const opts = { headers: authHeaders(), credentials: "same-origin" };
    return fetch(BASE + "/api" + path, opts).then((r) => {
      if (!r.ok) return r.json().catch(() => ({})).then((j) => {
        const d = j && j.detail;
        throw new Error(d && typeof d === "object" ? d.message || JSON.stringify(d)
          : d || r.status);
      });
      const cd = r.headers.get("content-disposition") || "";
      const m = /filename="?([^";]+)"?/.exec(cd);
      return r.blob().then((b) => {
        const url = URL.createObjectURL(b);
        const a = document.createElement("a");
        a.href = url;
        a.download = (m && m[1]) || fallbackName || "download";
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      });
    });
  }

  /* 极简口径:中性灰打底、无渐变、饱和度整体压低,但强调色与图标保留色相 ——
     一个界面全灰之后,"哪里能点"就只能靠位置记忆,那是拿可用性换观感。

     强调色是赭石,不是原来那个 teal(#14b8a6)。原配色整套照的是 sub2api
     (它的 tailwind.config.js 里 primary 就是 teal-500 那一列,连 logo 渐变
     135deg #14b8a6→#0d9488 都同一条),同类项目一眼认得出来。换色相是为了
     不再撞脸,顺带避开 new-api 那系的紫;暖色在这类网关里几乎没人用,
     压在中性灰上对比也明确。

     warning 从琥珀 #b45309 往黄挪了一档:赭石本身就在橙色区,两者色相差
     十几度,「警告」标签摆在主按钮旁边会看成同一个东西。 */
  const overrides = {
    common: {
      primaryColor: "#a8613c", primaryColorHover: "#bd7248",
      primaryColorPressed: "#8f5031", primaryColorSuppl: "#9d5a37",
      infoColor: "#426782", successColor: "#24734e",
      warningColor: "#936b12", errorColor: "#ba423b",
      bodyColor: "#f5f5f3", cardColor: "#ffffff", modalColor: "#ffffff",
      popoverColor: "#ffffff", textColorBase: "#242722",
      textColor1: "#242722", textColor2: "#555b54", textColor3: "#697163",
      placeholderColor: "#71796c", textColorDisabled: "#838c7d",
      borderColor: "#e6e8e2", dividerColor: "#eceee8",
      borderRadius: "10px", borderRadiusSmall: "7px", fontSize: "14px",
      fontFamily: '"Manrope","PingFang SC","Microsoft YaHei UI","Microsoft YaHei",sans-serif',
      fontFamilyMono: "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace",
    },
    Card: { borderRadius: "18px", titleFontSizeSmall: "15px", titleFontWeight: "650" },
    Button: { fontWeight: "600", heightMedium: "40px", heightSmall: "34px" },
    Input: { color: "#f7f8f5", colorFocus: "#ffffff", heightMedium: "40px" },
    DataTable: { thColor: "#f8f9f6", tdColorHover: "#f7f8f4", thFontWeight: "600" },
    Layout: { color: "#f5f5f3", siderColor: "#fafbf8" },
    Menu: { itemHeight: "44px", borderRadius: "10px", itemColorActive: "#eee4db",
      itemColorActiveHover: "#e8dbce", itemTextColorActive: "#884b2d",
      itemIconColorActive: "#884b2d" },
  };
  /* 深色主题共用同一个色相,只把亮度往上抬:低饱和色是按白底挑的,压在近黑底上
     对比度不够(#a8613c 约 3.5:1,选中态的小字会发灰)。不换色相 ——
     主按钮在两套主题下必须是同一个颜色,否则那是两个品牌。 */
  const overridesDark = {
    common: Object.assign({}, overrides.common, {
      primaryColor: "#d99a72", primaryColorHover: "#e5b092",
      primaryColorPressed: "#c98860", primaryColorSuppl: "#d1906a",
      infoColor: "#91b7d2", successColor: "#79c6a0",
      warningColor: "#e4be65", errorColor: "#f08a80",
      bodyColor: "#151815", cardColor: "#1e221e", modalColor: "#222722",
      popoverColor: "#282e28", textColorBase: "#ebeee8",
      textColor1: "#ebeee8", textColor2: "#bdc5b9", textColor3: "#939e8e",
      placeholderColor: "#a1ac9b", textColorDisabled: "#86937e",
      borderColor: "#343c32", dividerColor: "#30382e",
    }),
    Card: overrides.Card,
    Button: overrides.Button,
    Input: { color: "#191d19", colorFocus: "#222722", heightMedium: "40px" },
    DataTable: { thColor: "#252b23", tdColorHover: "#272f25", thFontWeight: "600" },
    Layout: { color: "#151815", siderColor: "#1b201a" },
    Menu: { itemHeight: "44px", borderRadius: "10px", itemColorActive: "#392d23",
      itemColorActiveHover: "#473429", itemTextColorActive: "#edb78f",
      itemIconColorActive: "#edb78f" },
  };
  /* 主题只有一个来源:这个 ref。html.dark 供页面级 CSS 用,localStorage 供刷新前置脚本用。 */
  const dark = ref(localStorage.getItem(THEME_KEY) === "dark");
  watchEffect(() => {
    localStorage.setItem(THEME_KEY, dark.value ? "dark" : "light");
    document.documentElement.classList.toggle("dark", dark.value);
  });
  const theme = computed(() => (dark.value ? naive.darkTheme : null));
  const themeOverrides = computed(() => (dark.value ? overridesDark : overrides));

  /* 窄屏(手机)只有一个来源:这个 ref。外壳拿它把侧栏改成抽屉,页面拿它决定
     那些「桌面上刚好、手机上挤爆」的控件换什么形态(比如 segment 页签在 390px
     上七个标签会糊成一条、点不到后面几个,得换成能横滑的 line)。
     断点与 portal.html 里那条 media query 必须同一个值,否则会出现「CSS 已经
     按窄屏排、JS 还当宽屏」的错位。 */
  const NARROW_MQ = window.matchMedia("(max-width: 860px)");
  const narrow = ref(NARROW_MQ.matches);
  NARROW_MQ.addEventListener("change", (e) => { narrow.value = e.matches; });

  /* 离散 API:模块级也能弹提示,省掉 message/dialog provider 的嵌套。 */
  const discrete = naive.createDiscreteApi(
    ["message", "dialog", "notification"],
    { configProviderProps: computed(() => ({
        theme: theme.value, themeOverrides: themeOverrides.value,
        locale: naive.zhCN, dateLocale: naive.dateZhCN })) });
  const msg = discrete.message;
  const dlg = discrete.dialog;

  function copy(text, label) {
    const done = () => { msg.success((label || "已复制") + ""); return true; };
    if (navigator.clipboard && location.protocol === "https:") {
      return navigator.clipboard.writeText(text).then(done, () => fallback(text, done));
    }
    return Promise.resolve(fallback(text, done));
  }
  function fallback(text, done) {
    const previousFocus = document.activeElement;
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.cssText = "position:fixed;top:-9999px";
    document.body.appendChild(ta);
    ta.select();
    try {
      if (!document.execCommand("copy")) throw new Error("copy rejected");
      return done();
    } catch (e) { msg.error("复制失败,请手动选中"); return false; }
    finally {
      document.body.removeChild(ta);
      previousFocus?.focus({ preventScroll: true });
    }
  }

  const nf = (n) => Number(n || 0).toLocaleString("en-US");
  const usd = (v) => ((v || 0) < 0 ? "-$" : "$") + Math.abs(v || 0).toFixed(4);
  /* 数量摘要统一使用十进制 K / M / B；明细仍用 nf 保留完整计量。
     最多两位小数，舍入到 1000 时再进一档，避免 1000K / 1000M。 */
  function kf(n) {
    if (n == null || n === "") return "—";
    const value = Number(n);
    if (!Number.isFinite(value)) return "—";
    const magnitude = Math.abs(value);
    if (magnitude < 1000) return String(value);
    const units = ["K", "M", "B"];
    let unit = 0, scale = 1000;
    while (unit < units.length - 1 && magnitude >= scale * 1000) {
      unit += 1; scale *= 1000;
    }
    let rounded = Math.round(magnitude / (scale / 100)) / 100;
    if (rounded >= 1000 && unit < units.length - 1) {
      unit += 1; scale *= 1000;
      rounded = Math.round(magnitude / (scale / 100)) / 100;
    }
    return (value < 0 ? "-" : "") + rounded + units[unit];
  }
  /* 小额消费用 6 位才看得见,大额用 2 位才不刺眼。 */
  const fmt = (n, digits) => {
    const v = Number(n || 0);
    if (v === 0) return (0).toFixed(digits == null ? 2 : digits);
    if (digits != null) return v.toFixed(digits);
    return Math.abs(v) < 0.01 ? v.toFixed(6) : v.toFixed(2);
  };
  /* 等宽栈直接取 overrides 里的那一份:Naive 只在少数组件内部生成
     --n-font-family-mono,普通 div/td 上取不到,写 var() 会静默回落成无衬线。 */
  const MONO = { fontFamily: overrides.common.fontFamilyMono,
    fontVariantNumeric: "tabular-nums" };
  const SUBSTY = Object.assign({ fontSize: "11px", lineHeight: "1.25" }, MONO);
  const pad = (n) => (n < 10 ? "0" + n : String(n));
  function absTime(ts) {
    if (!ts) return "—";
    const d = new Date(ts * 1000);
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) +
      " " + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }
  const dayTime = (ts) => (ts ? absTime(ts).slice(5) : "—");
  function relTime(ts) {
    if (!ts) return "从未";
    const s = Math.floor(Date.now() / 1000) - ts;
    if (s < 60) return "刚刚";
    if (s < 3600) return Math.floor(s / 60) + " 分钟前";
    if (s < 86400) return Math.floor(s / 3600) + " 小时前";
    if (s < 30 * 86400) return Math.floor(s / 86400) + " 天前";
    return absTime(ts).slice(0, 10);
  }

  /* 时长卡的三种文案。管理台与用户端都要用,放在共用层免得两边各写一套、
     哪天改了口径只改一边。duration_hours=0 一律是「不限时」。 */
  const planDur = (hours) => {
    const n = Number(hours) || 0;
    if (!n) return "不限时";
    if (n % 720 === 0) return (n / 720) + " 个月";
    if (n % 168 === 0) return (n / 168) + " 周";
    if (n % 24 === 0) return (n / 24) + " 天";
    return n + " 小时";
  };
  /* 卡种。按「一张卡管多久」分档,72 小时算天卡、240 小时算周卡。 */
  const planKind = (hours) => {
    const n = Number(hours) || 0;
    if (!n) return "不限时卡";
    if (n < 24) return "小时卡";
    if (n < 168) return "天卡";
    if (n < 720) return "周卡";
    return "月卡";
  };
  /* 剩余时长。到期时间为 0 表示不限时;已过期给「已到期」而不是负数。 */
  const planLeft = (expiresAt) => {
    if (!expiresAt) return "不限时";
    const s = expiresAt - Math.floor(Date.now() / 1000);
    if (s <= 0) return "已到期";
    if (s < 3600) return Math.ceil(s / 60) + " 分钟";
    if (s < 86400) return Math.floor(s / 3600) + " 小时 " +
      Math.floor((s % 3600) / 60) + " 分钟";
    return Math.floor(s / 86400) + " 天 " + Math.floor((s % 86400) / 3600) + " 小时";
  };

  const ST = {
    active: ["success", "启用"], disabled: ["error", "已禁用"],
    expired: ["warning", "已过期"], completed: ["success", "已完成"],
    unused: ["success", "未使用"], used: ["default", "已使用"],
    pending: ["warning", "待支付"], paid: ["info", "已支付"],
    recharging: ["info", "到账中"], failed: ["error", "失败"],
  };
  const st = (s) => ST[s] || ["default", s || "—"];

  const POLICY = { balance: "余额扣费", quota: "额度限额", free: "免费" };
  const MODE = { token: "按 Token", per_request: "按次", free: "免费" };
  /* 公告级别:后端只存 info/success/warning/error 四种,与 Naive 的 type 同名,
     所以一个映射同时供 n-tag、n-alert 和铃铛圆点用。 */
  const ANN_LEVEL = { info: "通知", success: "好消息", warning: "注意",
    error: "重要" };
  const ANN_MODE = { silent: "仅铃铛", popup: "登录弹窗" };

  /* 流水来源:core 里 reason 是自由字符串,认识的给中文,不认识的原样显示。
     用户端与管理台共用一份,免得同一笔流水两处显示成两种词。 */
  const REASON = { recharge: ["info", "充值到账"], redeem: ["success", "兑换码"],
    affiliate: ["warning", "邀请返佣"], admin: ["error", "管理员调整"],
    checkin: ["success", "每日签到"], usage: ["default", "调用消费"] };
  const reasonOf = (r) => REASON[r] || ["default", r || "—"];

  const tag = (t, txt) => h(naive.NTag,
    { size: "small", round: true, bordered: false, type: t }, () => txt);
  /* 金额不锁 2 位:一笔 $0.000107 的消费流水在两位小数下会显示成 -$0.00,
     看着像没扣钱。走 fmt 的自适应精度,小额自动给 6 位。 */
  /* 内联语义色。naive 的 successColor 之类只覆盖组件内部,写在 style 里的颜色
     得自己按主题选一档:#15803d 压在近黑底上只有 2.5:1,小字读不出来。
     深色档与 overridesDark 里的语义色取同一组值,免得同一个「成功」在标签里
     一个绿、在流水数字上另一个绿。 */
  const SEM = { success: ["#24734e", "#79c6a0"], warning: ["#936b12", "#e4be65"],
    error: ["#ba423b", "#f08a80"] };
  const sem = (k) => (SEM[k] || SEM.success)[dark.value ? 1 : 0];
  /* 调用方传进来的那些低饱和 hex 也是按白底挑的,深色下同样偏暗。这里用 filter
     整体提亮一档,而不是给每个调用点再维护一套深色 hex —— 卡片图标与强调数字
     加起来十几个调用点各传各的色,两套表迟早歪成一深一浅。 */
  const litUp = () => (dark.value
    ? { filter: "brightness(1.5) saturate(1.05)" } : null);
  const money = (v) => h("span", { style: { color: v >= 0 ? sem("success") : "inherit",
    fontVariantNumeric: "tabular-nums", fontWeight: 550 } },
    (v >= 0 ? "+$" : "-$") + fmt(Math.abs(v)));

  /* 供应商识别:模型名 → [正则, 主色, 名称, 字母, 图标文件, 是否单色]。
     图标是 @lobehub/icons-static-svg(MIT)里挑出来的官方品牌图,已落到
     static/model-icons/,不走 CDN。没有对应图标的落回字母牌。
     单色图(用 fill="currentColor" 的图标)在 <img> 里会解析成黑,
     深色主题下要反色才看得见 —— 见 provIcon。
     顺序即优先级,第一个命中的胜出。 */
  const PROV = [
    [/gpt|chatgpt|^o[134]-/, "#10a37f", "OpenAI", "O", "openai", 1],
    /* Claude 系模型对外可能被叫成 mp-fable-5 / mp-opus-4.8 / mp-sonnet-5 这类
       绰号,名字里没有 claude,单靠 /claude/ 会全部漏成「自建」。 */
    [/claude|anthropic|fable|opus|sonnet/, "#d97757", "Anthropic", "A",
     "claude-color"],
    [/gemma/, "#4285f4", "Gemma", "G", "gemma-color"],
    [/gemini|learnlm/, "#4285f4", "Google", "G", "gemini-color"],
    /* veo / nano-banana 是 Google 的视频与图像模型,名字里不带 gemini。 */
    [/veo-|nano-banana/, "#4285f4", "Google", "G", "google-color"],
        [/minimax|abab/, "#f43f5e", "MiniMax", "M", "minimax-color"],
    [/deepseek/, "#4d6bfe", "DeepSeek", "D", "deepseek-color"],
    [/qwen|qwq/, "#7c5cff", "Qwen", "Q", "qwen-color"],
    [/glm|chatglm/, "#3859ff", "Zhipu", "Z", "zhipu-color"],
    /* 用 Moonshot 的公司标而不是 kimi-color:后者是蓝底白字的应用图标,
       主体是白的,放在浅色卡片上只剩一个蓝点。 */
    [/kimi|moonshot/, "#0f172a", "Moonshot", "K", "moonshot", 1],
    [/step-|stepfun/, "#005ce7", "StepFun", "S", "stepfun-color"],
    [/mercury|inception/, "#18181b", "Inception", "I", "inception", 1],
    [/kling/, "#0d1f3c", "Kling", "K", "kling-color"],
    [/seedream|seedance/, "#325ab4", "ByteDance", "B", "bytedance-color"],
    [/\bwan-/, "#ff6a00", "Alibaba", "W", "alibaba-color"],
    [/pixverse/, "#7b3aed", "PixVerse", "P", "pixverse-color"],
      /* 取单色版:poolside-color 是「蓝底白字」的应用图标,主体是白的,
       压在浅色卡片上只剩个角标(kimi-color 同款毛病)。 */
    [/poolside|laguna/, "#4137ff", "Poolside", "P", "poolside", 1],
  ];
  const prov = (m) => {
    const s = String(m || "").toLowerCase();
    for (let k = 0; k < PROV.length; k++)
      if (PROV[k][0].test(s))
        return { c: PROV[k][1], n: PROV[k][2], l: PROV[k][3],
                 i: PROV[k][4], mono: !!PROV[k][5] };
    return { c: "#94a3b8", n: "自建",
      l: (s.replace(/[^a-z0-9]/g, "")[0] || "?").toUpperCase() };
  };
  /* 供应商图标方块:有官方图用图,没有就用主色字母牌。两条路尺寸一致,
     混排时不会一格高一格低。 */
  const provIcon = (m, size) => {
    const p = typeof m === "string" ? prov(m) : m;
    const px = (size || 15) + "px";
    if (p.i)
      return h("img", { src: "/static/model-icons/" + p.i + ".svg",
        alt: p.n, title: p.n, style: { width: px, height: px,
          borderRadius: "3px", flex: "0 0 auto", objectFit: "contain",
          filter: p.mono && dark.value ? "invert(1)" : null } });
    return h("span", { title: p.n, style: { display: "grid",
      placeItems: "center", width: px, height: px, borderRadius: "4px",
      flex: "0 0 auto", background: p.c, color: "#fff",
      fontSize: Math.round((size || 15) * 0.63) + "px", fontWeight: 800,
      lineHeight: 1 } }, p.l);
  };
  /* 内联 SVG:一个名字一条 path,零依赖零额外请求,专供「图 + 文字」并排。 */
  const ICONS = {
    pulse: "M3 12h4l2.5-7 4.5 14 2.5-7H21",
    network: "M9 3h6v5H9V3Zm-6 13h6v5H3v-5Zm12 0h6v5h-6v-5ZM12 8v4m-6 4v-4h12v4",
    users: "M15 20v-2a5 5 0 0 0-10 0v2m13-8a4 4 0 0 1 3 4v3M14 4a4 4 0 0 1 0 8M14 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z",
    ticket: "M3 5h18v5a2 2 0 0 0 0 4v5H3v-5a2 2 0 0 0 0-4V5Zm12 0v3m0 3v2m0 3v3",
    arrowRight: "M4 12h16m-6-6 6 6-6 6",
    cacheRead: "M4 6.5c0-1.4 3.6-2.5 8-2.5s8 1.1 8 2.5S16.4 9 12 9 4 7.9 4 6.5Z" +
      "m16 5.3c0 1.4-3.6 2.5-8 2.5s-8-1.1-8-2.5m16 5.2c0 1.4-3.6 2.5-8 2.5" +
      "s-8-1.1-8-2.5M4 6.5V17m16-10.5V17",
    cacheWrite: "M4.5 19.5h4L19 9a2.83 2.83 0 0 0-4-4L4.5 15.5v4Zm10-14 4 4",
    clock: "M12 7.2V12l3.4 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z",
    refresh: "M20 7v5h-5m5 0a8 8 0 1 0-2.3 5.7",
    bolt: "M13.2 3 5.5 14h5.6l-.9 7 7.9-11h-5.7l.8-7Z",
    dollar: "M12 3.2v17.6M15.8 7.4c0-1.6-1.7-2.4-3.8-2.4s-3.8.8-3.8 2.5S9.9 10 12 10" +
      "s4 1 4 2.7-1.7 2.6-4 2.6-4-.9-4-2.6",
    cube: "M20 7.1 12 3.2 4 7.1m16 0-8 3.9m8-3.9v9.8l-8 3.9m0-13.7L4 7.1m8 3.9v9.8" +
      "M4 7.1v9.8l8 3.9",
    doc: "M4 14.5h4l1.2 2.2h5.6L16 14.5h4M4 14.5l2.6-7A2 2 0 0 1 8.5 6h7a2 2 0 0 1 " +
      "1.9 1.5l2.6 7v3.5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-3.5Z",
    fire: "M12 20.8c3.6 0 6-2.3 6-5.4 0-3.9-3.4-5.3-2.9-9.2-2.4 1-3.9 2.9-3.9 4.9 0 " +
      "0-1.4-1-1.4-2.9C7.9 9.7 6 11.6 6 15.4c0 3.1 2.4 5.4 6 5.4Z",
    xCircle: "M9.4 9.4l5.2 5.2m0-5.2-5.2 5.2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z",
    alert: "M12 9.5v3.6m0 3h.01M10.3 4.2 2.7 17.3a2 2 0 0 0 1.7 3h15.2a2 2 0 0 0 " +
      "1.7-3L13.7 4.2a2 2 0 0 0-3.4 0Z",
    tag: "M7.4 7.4h.01M3 12.6V5.4A2.4 2.4 0 0 1 5.4 3h7.2c.6 0 1.2.3 1.7.7l6 6a2.4 " +
      "2.4 0 0 1 0 3.4l-6.8 6.8a2.4 2.4 0 0 1-3.4 0l-6-6c-.4-.5-.7-1.1-.7-1.7Z",
    key: "M15.5 7.5a4 4 0 1 1-3.7 5.5H8.5v2.8H6v2.7H3v-3.5l8.8-8.8a4 4 0 0 1 3.7 1.3Z",
    user: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm0 0c-3.6 0-6.5 2.6-6.5 5.8V20h13v-2.2" +
      "c0-3.2-2.9-5.8-6.5-5.8Z",
    gift: "M4 11h16M4 11v8a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-8M4 11V8h16v3M12 20V8" +
      "m0 0S11 4 8.5 4a2 2 0 0 0 0 4H12Zm0 0s1-4 3.5-4a2 2 0 0 1 0 4H12Z",
    gear: "M12 15.2a3.2 3.2 0 1 0 0-6.4 3.2 3.2 0 0 0 0 6.4Zm7.4-3.2c0 .5 0 1-.1 1.4" +
      "l2 1.5-1.9 3.3-2.4-1a7.4 7.4 0 0 1-2.4 1.4l-.3 2.5h-3.8l-.3-2.5a7.4 7.4 0 0 1" +
      "-2.4-1.4l-2.4 1L3.5 15l2-1.5a8 8 0 0 1 0-2.9L3.5 9l1.9-3.3 2.4 1a7.4 7.4 0 0 1 " +
      "2.4-1.4l.3-2.5h3.8l.3 2.5a7.4 7.4 0 0 1 2.4 1.4l2.4-1L21.3 9l-2 1.5c.1.5.1 1 .1 1.5Z",
    grid: "M4 4h6v6H4V4Zm10 0h6v6h-6V4ZM4 14h6v6H4v-6Zm10 0h6v6h-6v-6Z",
    link: "M10.5 13.5a3.5 3.5 0 0 0 5 0l3-3a3.5 3.5 0 0 0-5-5l-1.7 1.7M13.5 10.5a3.5 " +
      "3.5 0 0 0-5 0l-3 3a3.5 3.5 0 0 0 5 5l1.7-1.7",
    /* 共享 Key:托盘 + 上行箭头(交上来换奖励) */
    share: "M12 15.5V4m0 0L8.3 7.7M12 4l3.7 3.7M5 13.5V19a1 1 0 0 0 1 1h12a1 1 0 " +
      "0 0 1-1v-5.5",
    wallet: "M3 8.5h14.5a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-8Zm0 0V7a2 2 " +
      "0 0 1 2-2h10.5v3.5M16 13.5h.01",
    /* 下面五个专供图标按钮:文字按钮换成图标后,靠形状认功能,别改几何。 */
    copy: "M10 9.5h9a1.5 1.5 0 0 1 1.5 1.5v9a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 " +
      "8.5 20.5v-9A1.5 1.5 0 0 1 10 9.5ZM15.5 9.5V5a1.5 1.5 0 0 0-1.5-1.5H5A1.5 " +
      "1.5 0 0 0 3.5 5v9A1.5 1.5 0 0 0 5 15.5h3.5",
    pencil: "M5 19h3.8L19.4 8.4a2.4 2.4 0 0 0-3.4-3.4L5 15.6V19ZM15.4 5.6l3.4 3.4",
    trash: "M4.5 7.5h15M9.5 7.5V5a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v2.5M6.6 7.5l.8 11.6a1.5 " +
      "1.5 0 0 0 1.5 1.4h6.2a1.5 1.5 0 0 0 1.5-1.4l.8-11.6M10.5 11v6M13.5 11v6",
    ban: "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0ZM5.6 5.6 18.4 18.4",
    check: "m4.5 12.8 5 5 10-11.2",
    circleCheck: "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-13-1 3 3 5-5",
    circleX: "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0ZM9 9l6 6m0-6-6 6",
    arrowDown: "M12 4v16m-6-6 6 6 6-6",
    arrowUp: "M12 20V4m-6 6 6-6 6 6",
    database: "M4 6c0-4 16-4 16 0S4 10 4 6Zm0 0v12c0 4 16 4 16 0V6M4 12c0 4 16 4 16 0",
    databaseRead: "M3 5c0-3 12-3 12 0S3 8 3 5Zm0 0v12c0 3 8 3 10 2M15 5v4" +
      "M3 10c0 2 6 3 9 2M3 15c0 2 4 3 7 3M14 15h8m-3-3 3 3-3 3",
    databaseWrite: "M3 5c0-3 12-3 12 0S3 8 3 5Zm0 0v12c0 3 8 3 10 2M15 5v4" +
      "M3 10c0 2 6 3 9 2M3 15c0 2 4 3 7 3M22 15h-8m3-3-3 3 3 3",
    stream: "M3 12h3l3-7 6 14 3-7h3",
    response: "M4 5h16v12H9l-5 4V5Zm4 4h8m-8 4h5",
    eye: "M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Zm13 0a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z",
    chevronUp: "m6.5 14.8 5.5-5.6 5.5 5.6",
    chevronDown: "m6.5 9.2 5.5 5.6 5.5-5.6",
    chevronRight: "m9 5 7 7-7 7",
    sun: "M12 4.2V2.6m0 18.8v-1.6M6.5 6.5 5.4 5.4m13.2 13.2-1.1-1.1M4.2 12H2.6" +
      "m18.8 0h-1.6M6.5 17.5l-1.1 1.1M18.6 5.4l-1.1 1.1M16.2 12a4.2 4.2 0 1 1" +
      "-8.4 0 4.2 4.2 0 0 1 8.4 0Z",
    moon: "M20 14.4A8.6 8.6 0 0 1 9.6 4 8.4 8.4 0 1 0 20 14.4Z",
    logout: "M14.5 8.2V5.6a1.6 1.6 0 0 0-1.6-1.6H5.6A1.6 1.6 0 0 0 4 5.6v12.8" +
      "a1.6 1.6 0 0 0 1.6 1.6h7.3a1.6 1.6 0 0 0 1.6-1.6v-2.6M9.5 12H21m0 0" +
      "-3.4-3.4M21 12l-3.4 3.4",
    bars: "M5 20V13.5m7 6.5V4m7 16v-9",
    bell: "M18 8.8a6 6 0 1 0-12 0c0 4.7-2 6.2-2 6.2h16s-2-1.5-2-6.2M13.8 18.8" +
      "a2 2 0 0 1-3.6 0",
    gauge: "M12 13.5 15.6 9M21 13.5a9 9 0 1 0-18 0",
    coins: "M4 7.2c0-1.3 3.6-2.4 8-2.4s8 1.1 8 2.4-3.6 2.4-8 2.4-8-1.1-8-2.4Z" +
      "M4 7.2v9.6c0 1.3 3.6 2.4 8 2.4s8-1.1 8-2.4V7.2M4 12c0 1.3 3.6 2.4 8 2.4" +
      "s8-1.1 8-2.4",
    mail: "M3.5 7.5h17v9a1.5 1.5 0 0 1-1.5 1.5H5a1.5 1.5 0 0 1-1.5-1.5v-9Z" +
      "m0 .3 8.5 5.4 8.5-5.4",
    image: "M4 6.5A1.5 1.5 0 0 1 5.5 5h13A1.5 1.5 0 0 1 20 6.5v11A1.5 1.5 0 0 1 " +
      "18.5 19h-13A1.5 1.5 0 0 1 4 17.5v-11Zm0 9.5 4.3-4.3 3.2 3.2 3-3L20 15" +
      "M9 9.8h.01",
    shield: "M12 3.6 5 6v5.4c0 4 2.8 7.4 7 9 4.2-1.6 7-5 7-9V6l-7-2.4Z" +
      "m-2.6 8.6 2 2 3.6-4",
    search: "M10.8 17.6a6.8 6.8 0 1 0 0-13.6 6.8 6.8 0 0 0 0 13.6Zm4.9-.2 4 4",
    sliders: "M4 8.5h9m4.5 0H20M4 15.5h2.5m4.5 0H20M13 8.5a2.2 2.2 0 1 0 4.5 0 " +
      "2.2 2.2 0 0 0-4.5 0Zm-6.5 7a2.2 2.2 0 1 0 4.5 0 2.2 2.2 0 0 0-4.5 0Z",
    /* 窄屏顶栏的导航开关。bars 是柱状图不是汉堡,别混用。 */
    menu: "M4 7h16M4 12h16M4 17h16",
  };
  const ic = (name, size, color, sw) => h("svg", { viewBox: "0 0 24 24",
    class: "console-icon", "aria-hidden": "true", focusable: "false",
    width: size || 13, height: size || 13, fill: "none", stroke: "currentColor",
    "stroke-width": sw || 1.75, "stroke-linecap": "round", "stroke-linejoin": "round",
    style: { color: color, flex: "0 0 auto", display: "block" } },
    h("path", { d: ICONS[name] }));
  const Ic = { props: ["name", "size", "color", "sw"],
    render() { return ic(this.name, this.size, this.color, this.sw); } };
  /* 六个通道色:表格用中间档,浮层(CT)用浅一档 —— 浮层底色恒深,不跟主题走。
     刻意保留可区分的色相:全压成灰阶之后,堆叠柱与环形图相邻色块就只能靠
     tooltip 读,那是拿数据可读性换观感。饱和度统一压低让它们退到背景里,
     但亮度必须卡在中间档 —— 表格这一份要同时压在白底和近黑底上,
     再往深走一档(接近正文色那种深度)在深色主题里就糊得看不见了。
     cost 跟强调色同族(赭石):消费是这个站最常看的一列,让它和主按钮一个色系。
     cw 从琥珀挪到橄榄黄、err 往正红收:强调色换成赭石之后,原来那两个色
     跟它都只差一二十度色相,同一张堆叠图里「缓存写」「失败」「消费」会连成一片。 */
  const C = { in: "#5b8a6b", out: "#7d6da6", cr: "#4d7fa0", cw: "#97882f",
    cost: "#a8613c", ctx: "#5b80b5", err: "#bf4038" };
  const CT = { in: "#7fae90", out: "#9d8fc4", cr: "#79a3c0", cw: "#bfae5c",
    cost: "#d99a72", ctx: "#8fadd6", err: "#ef8080" };

  const tipRow = (label, value, color, top) => h("div", { style: Object.assign(
    { display: "flex", justifyContent: "space-between", gap: "20px" },
    top ? { borderTop: "1px solid rgba(255,255,255,.22)", marginTop: "3px",
      paddingTop: "4px" } : null) }, [
    h("span", { style: { opacity: 0.62 } }, label),
    h("span", { style: Object.assign({ fontWeight: 650, color: color }, MONO) },
      value)]);

  /* 悬浮明细挂在已有文字上,不再额外摆一个 info 圆点占位。 */
  const wrapTip = (node, rows, place) => h(naive.NTooltip,
    { placement: place || "left" }, {
    trigger: () => node,
    default: () => h("div", { style: { display: "flex", flexDirection: "column",
      gap: "3px", fontSize: "11.5px", minWidth: "182px" } }, rows),
  });

  const subline = (c) => (typeof c === "string"
    ? h(naive.NText, { depth: 3, style: SUBSTY }, () => c) : c);
  const stack = (top, bottom, align) => h("div", { style: { display: "flex",
    flexDirection: "column", alignItems: align || "flex-start", gap: "2px" } },
    [top, bottom == null ? null : subline(bottom)]);

  /* 统一描边小方块:模型 / 密钥 / 费用共用一种视觉,避免每列各造一套。 */
  const box = (kids, extra) => h("span", { style: Object.assign({
    display: "inline-flex", alignItems: "center", gap: "5px", height: "21px",
    padding: "0 7px", borderRadius: "6px", fontSize: "12px", fontWeight: 600,
    border: "1px solid rgba(127,127,127,.22)",
    background: "rgba(127,127,127,.08)" }, MONO, extra) }, kids);

  const modelChip = (m) => box([
    provIcon(m),
    h("span", { style: { whiteSpace: "nowrap" } }, m),
  ], { paddingLeft: "4px" });

  /* 费用只留一枚描边胶囊:数字本身就是重点,不用图标再抢一次注意力。 */
  const costChip = (txt, color) => box([
    color ? null : h("span", { style: { opacity: 0.5, fontWeight: 600 } }, "$"),
    h("span", null, txt),
  ], { fontSize: "12.5px", fontWeight: 700, padding: "0 8px", color: color,
    borderColor: color ? color + "55" : undefined,
    background: color ? color + "18" : undefined });

  /* 耗时两行各配一条 2px 竖条,颜色逐行判定,比并排两枚彩色 chip 安静得多。 */
  const timeRow = (label, val, lv) => h("div", { style: { display: "flex",
    alignItems: "center", gap: "6px" } }, [
    h("span", { style: { width: "2px", height: "11px", borderRadius: "99px",
      flex: "0 0 auto",
      background: SEM[lv] ? sem(lv) : "rgba(127,127,127,.4)" } }),
    h("span", { style: { fontSize: "11px", opacity: 0.56 } }, label),
    h("span", { style: Object.assign({ fontSize: "12px", fontWeight: 650,
      color: SEM[lv] ? sem(lv) : undefined }, MONO) }, val)]);

  /* 统计胶囊:一条竖色条 + 标签 + 数值,不放图标,把注意力留给上方大卡片。 */
  const pill = (label, value, color) => h("span", { style: {
    display: "inline-flex", alignItems: "center", gap: "6px", height: "24px",
    padding: "0 9px", borderRadius: "7px", fontSize: "11.5px",
    border: "1px solid rgba(127,127,127,.2)",
    background: "rgba(127,127,127,.06)" } }, [
    h("span", { style: { width: "2px", height: "12px", borderRadius: "99px",
      flex: "0 0 auto", background: color } }),
    h("span", { style: { opacity: 0.6 } }, label),
    h("span", { style: Object.assign({ fontWeight: 650, fontSize: "12px" },
      MONO) }, value)]);
  const Pills = { props: ["items"], render() {
    return h(naive.NSpace, { size: 6, wrap: true, align: "center" },
      () => this.items.map((x) => pill(x[0], x[1], x[2])));
  } };

  /* 指标的层级固定为名称、数值、口径,与各页面共用一套视觉。
     value2 给「今日 / 累计」这种主副双值:副值同色系但降饱和,不抢主值。
     rows 给需要两行指标的卡(如 RPM / TPM),此时不显示 value。 */
  /* 标题按词揭示，首字只作轻量强调；可访问名称始终是完整标题。 */
  const PageTitle = {
    props: ["text"],
    render() {
      const text = String(this.text || "");
      const words = Array.from(new Intl.Segmenter("zh-CN", { granularity: "word" }).segment(text));
      return h("span", { class: "console-title", "aria-label": text }, words.map((word, i) =>
        h("span", { class: "console-title-mask", "aria-hidden": "true", key: text + i }, [
          h("span", { class: "console-title-word", style: { "--word-delay": Math.min(i, 3) * 24 + "ms" } },
            i === 0 ? [h("span", { class: "console-title-initial" }, Array.from(word.segment)[0]),
              Array.from(word.segment).slice(1).join("")] : word.segment),
        ])));
    },
  };

  /* 默认只拆分展示，保留原始精度。只有明确 opt-in 的非金额统计才动：
     首次进入视区短促递增，真实更新翻动变化的位数，重渲染/滚动不重播。 */
  const MetricValue = {
    props: { value: [String, Number], animate: Boolean },
    setup(props) {
      const root = ref(null), intermediate = ref(null), previous = ref(""), changed = ref(false);
      const text = computed(() => String(props.value == null ? "—" : props.value));
      const parts = computed(() => /^([+\-]?[$¥]?)([\d,]+(?:\.\d+)?)(.*)$/.exec(text.value));
      // 白名单仅允许计数与缩写数量；金额、百分比、时长均保持稳定。
      const eligible = computed(() => props.animate && /^(?:\d[\d,]*)(?:\.\d+)?(?:[KMB]| 项模型)?$/.test(text.value));
      const media = window.matchMedia("(prefers-reduced-motion: reduce)");
      const reduced = ref(media.matches);
      let observer, frame = 0, timer = 0, visible = false, entered = false;
      const stop = () => {
        cancelAnimationFrame(frame); clearTimeout(timer);
        intermediate.value = null; changed.value = false;
      };
      const enter = () => {
        if (entered) return;
        entered = true;
        if (!eligible.value || reduced.value || !parts.value) return;
        const final = parts.value[2], amount = Number(final.replace(/,/g, ""));
        if (!Number.isFinite(amount) || amount === 0) return;
        const decimals = (final.split(".")[1] || "").length;
        const formatter = new Intl.NumberFormat("en-US", { useGrouping: final.includes(","),
          minimumFractionDigits: decimals, maximumFractionDigits: decimals });
        const start = performance.now();
        intermediate.value = formatter.format(0);
        const tick = (now) => {
          const progress = Math.min(1, (now - start) / 280);
          intermediate.value = progress < 1 ? formatter.format(amount * (1 - Math.pow(1 - progress, 3))) : null;
          if (progress < 1) frame = requestAnimationFrame(tick);
        };
        frame = requestAnimationFrame(tick);
      };
      const onMotion = (event) => { reduced.value = event.matches; if (event.matches) stop(); };
      onMounted(() => {
        media.addEventListener("change", onMotion);
        if (!props.animate) return;
        observer = new IntersectionObserver((entries) => {
          visible = entries[0].isIntersecting;
          if (visible) enter(); else stop();
        });
        observer.observe(root.value);
      });
      watch(text, (_, old) => {
        stop();
        if (!entered || !visible || reduced.value || !eligible.value) return;
        previous.value = old;
        changed.value = true;
        timer = setTimeout(() => { changed.value = false; }, 280);
      });
      onUnmounted(() => { stop(); observer?.disconnect(); media.removeEventListener("change", onMotion); });
      return () => {
        const match = parts.value;
        if (!match) return h("span", { ref: root, class: "console-value-text" }, text.value);
        const digits = match[2], before = previous.value.replace(/[^\d,.]/g, "").padStart(digits.length, " ");
        return h("span", { ref: root, class: ["console-number", { "is-updating": changed.value }],
          "aria-label": text.value, "data-motion": intermediate.value !== null ? "counting" : changed.value ? "updated" : "idle" }, [
          match[1] ? h("span", { class: "console-number-prefix", "aria-hidden": "true" }, match[1]) : null,
          h("span", { class: "console-number-digits", "aria-hidden": "true" }, [
            h("span", { class: "console-number-reserve" }, digits),
            h("span", { class: "console-number-display" }, intermediate.value !== null ? intermediate.value :
              Array.from(digits, (digit, i) => h("span", { key: i + ":" + digit,
                class: changed.value && before[i] !== digit ? "console-digit-changed" : null }, digit))),
          ]),
          match[3] ? h("span", { class: "console-number-unit", "aria-hidden": "true" }, match[3]) : null,
        ]);
      };
    },
  };
  const StatCard = {
    props: ["label", "value", "value2", "sub", "sub2", "color", "icon", "tint",
      "rows", "tone", "animate"],
    render() {
      const big = (txt) => h("span", { class: "console-metric-value" }, h(MetricValue, { value: txt, animate: this.animate }));
      let body;
      if (this.rows && this.rows.length) {
        body = h("div", { style: { display: "flex", flexDirection: "column",
          gap: "1px" } }, this.rows.map((r) => h("div", { style: {
            display: "flex", alignItems: "baseline", gap: "6px" } }, [
          h("span", { class: "console-metric-row-value" }, h(MetricValue, { value: r[1] })),
          h("span", { style: { fontSize: "11.5px", opacity: 0.55 } }, r[0]),
        ])));
      } else {
        body = h("div", { style: { display: "flex", alignItems: "baseline",
          gap: "5px", flexWrap: "wrap" } }, [
          big(this.value),
          this.value2 == null ? null : h("span", { class: "console-metric-secondary" },
            "/ " + this.value2),
        ]);
      }
      return h(naive.NCard, { size: "small", bordered: true,
        class: ["console-metric", this.tone ? "console-tone-" + this.tone : null], style: { height: "100%" } }, () => [
        h("div", { class: "console-metric-heading" }, [
          h("span", null, this.label),
          h("span", { class: "console-metric-icon" }, ic(this.icon, 18)),
        ]),
        body,
        this.sub == null ? null : h("div", { class: "console-metric-note" }, this.sub),
        this.sub2 == null ? null : h("div", { class: "console-metric-note" }, this.sub2),
      ]);
    },
  };
  /* 用户看的是等待时间:10 秒内绿、25 秒内黄，再慢才红。 */
  const elapsedLv = (s) => (s <= 10 ? "success"
    : s <= 25 ? "warning" : "error");
  const frtLv = (s) => (s <= 1.5 ? "success" : s <= 5 ? "warning" : "error");

  /* 缓存读写与请求 ID 从冻结快照读取；历史缺失字段给 null，不能冒充 0。 */
  function normUsage(row, keyNames) {
    const snap = row.pricing_snapshot || {};
    const mode = row.billing_mode || snap.mode || "free";
    const ctx = snap.total_ctx == null ? null : Number(snap.total_ctx);
    const inTok = row.input_tokens || 0;
    const cacheRead = snap.cache_read_tokens == null ? null : Number(snap.cache_read_tokens);
    // Old zero values conflated absent fields with measured zero; never infer writes from a read hit.
    const cacheWrite = snap.cache_write_tokens == null || (!snap.cache_usage_version && Number(snap.cache_write_tokens) === 0)
      ? null : Number(snap.cache_write_tokens);
    const cacheSplit = cacheRead != null && cacheWrite != null;
    const hasPx = snap.input_price != null || snap.output_price != null;
    const end = snap.end_reason;
    const ms = Math.max(0, Number(row.duration_ms) || 0);
    /* 非流请求只有完整 JSON 到达这一刻可观察，故客户端可见首字等于总耗时。
       这里同时兼容修复前没有 frt_ms 的历史非流记录。 */
    const frt = snap.frt_ms == null ? (!row.stream ? ms : null)
      : Number(snap.frt_ms);
    return {
      id: row.id,
      t: row.created_at,
      ip: snap.client_ip || null,
      keyId: row.api_key_id,
      key: (keyNames && keyNames[row.api_key_id]) ||
        (row.api_key_id ? "#" + row.api_key_id : "—"),
      channel: row.channel || "",
      model: row.model || "—",
      mode: mode,
      free: mode === "free",
      perReq: mode === "per_request",
      ratio: snap.rate_multiplier == null ? null : Number(snap.rate_multiplier),
      tier: snap.tier || null,
      thr: snap.long_threshold || null,
      i: inTok,
      o: row.output_tokens || 0,
      ctx: ctx,
      cacheRead: cacheRead,
      cacheWrite: cacheWrite,
      cacheWriteTTLValid: snap.cache_write_ttl_valid,
      cacheWrite30m: snap.cache_write_ttl_valid ? snap.cache_write_30m_tokens : null,
      cacheWrite12h: snap.cache_write_ttl_valid ? snap.cache_write_12h_tokens : null,
      cacheWriteUnclassified: snap.cache_write_ttl_valid ? snap.cache_write_unclassified_tokens : null,
      cacheSplit: cacheSplit,
      /* 旧快照只保留合计，不能据此猜测读写分配。 */
      cache: cacheSplit ? cacheRead + cacheWrite
        : ctx == null ? null : Math.max(0, ctx - inTok),
      px: hasPx ? { i: snap.input_price, o: snap.output_price,
        cr: snap.cache_read_price, cw: snap.cache_write_price,
        cw30: snap.cache_write_30m_price, cw12: snap.cache_write_12h_price } : null,
      perPrice: snap.per_request_price == null ? null : snap.per_request_price,
      units: snap.units || null,
      list: row.cost || 0,
      cost: row.actual_cost || 0,
      ms: ms,
      frt: frt,
      stream: !!row.stream,
      ok: !end || end === "done",
      end: end || null,
      rid: snap.request_id || null,
      policy: snap.policy || null,
      source: snap.source || null,
      tokenSource: ({ estimate: "估算", upstream: "上游实测", per_request: "按次" })[row.token_source] || row.token_source || null,
    };
  }
  const tpsOf = (r) => (r.ms > 0 && r.o > 0 ? Math.round(r.o / (r.ms / 1000)) : 0);
  const kindOf = (r) => (!r.ok ? ["失败", C.err]
    : r.free ? ["免费", sem("success")] : ["消耗", null]);
  const END_TEXT = { done: "正常结束", eof: "上游中断", client_gone: "客户端断开",
    scanner_error: "响应解析失败", idle_timeout: "长时间无输出" };

  /* 日志共用的紧凑单元格。按钮既是 hover 锚点，也是键盘/触屏说明入口；
     不把表格变成只能靠颜色和鼠标猜含义的图标墙。 */
  const UsageHint = {
    props: ["label", "cellClass", "activate"],
    setup(props, { slots }) {
      const show = ref(false);
      return () => h(naive.NTooltip, {
        show: show.value, placement: "top", trigger: "hover",
        "onUpdate:show": (v) => { show.value = v; },
        onClickoutside: () => { show.value = false; },
      }, {
        trigger: () => h("button", {
          type: "button", class: ["usage-hint", props.cellClass, props.activate ? "usage-action" : null],
          "aria-label": props.label,
          onFocus: () => { show.value = true; },
          onBlur: () => { show.value = false; },
          onKeydown: (e) => { if (e.key === "Escape") show.value = false; },
          onClick: () => {
            show.value = !props.activate;
            if (props.activate) props.activate();
          },
        }, h("span", { class: "usage-hint-content", "aria-hidden": "true" },
          slots.default())),
        default: () => h("div", { class: "usage-tip" }, slots.tip
          ? slots.tip() : props.label),
      });
    },
  };
  const usageHint = (label, content, options) => {
    const o = options || {};
    return h(UsageHint, { label: label, cellClass: o.class, activate: o.activate }, {
      default: () => content, tip: o.tip ? () => o.tip : undefined,
    });
  };
  const usageDatum = (icon, value, tone) => h("span", {
    class: "usage-datum", style: { color: SEM[tone] ? sem(tone)
      : (dark.value ? CT[tone] : C[tone]) },
  }, [ic(icon, 14), h("span", null, value)]);
  const usageTime = (r) => {
    const elapsed = (r.ms / 1000).toFixed(2) + " s";
    const first = r.frt == null ? "—" : (r.frt / 1000).toFixed(2) + " s";
    const speed = r.ms > 0 ? tpsOf(r) + " t/s" : "—";
    return usageHint("总耗时 " + elapsed + "；首字 " + first + "；速度 " + speed,
      h("span", { class: "usage-metrics" }, [
        usageDatum("clock", elapsed, elapsedLv(r.ms / 1000)),
        usageDatum("bolt", first, r.frt == null ? null : frtLv(r.frt / 1000)),
        usageDatum("gauge", speed),
      ]), { tip: [tipRow("总耗时", elapsed),
        tipRow(r.stream ? "首字延迟" : "首字（非流式完整响应）", first),
        tipRow("速度（输出 / 总耗时）", speed),
        h("div", { class: "usage-tip-note" }, "耗时：≤10s 绿 · ≤25s 黄 · >25s 红"),
        r.frt == null ? h("div", { class: "usage-tip-note" }, "此记录没有首字数据") : null,
      ] });
  };
  const usageTokens = (r) => {
    const cache = r.cache == null ? "—" : nf(r.cache);
    const read = r.cacheRead == null ? "—" : nf(r.cacheRead);
    const write = r.cacheWrite == null ? "—" : nf(r.cacheWrite);
    /* 两层账本：上层请求用量，下层缓存读写。方向图标承担标签，数值独立对齐。 */
    const datum = (icon, value, tone, cached) => h("span", {
      class: ["usage-token-datum", cached ? "usage-token-cache" : null,
        cached && (value == null || value === 0) ? "is-muted" : null],
    }, [
      ic(icon, cached ? 18 : 14, dark.value ? CT[tone] : C[tone]),
      h("span", { class: "usage-token-value" }, value == null ? "—" : nf(value)),
    ]);
    return usageHint("输入 " + nf(r.i) + "；输出 " + nf(r.o) +
      "；缓存读取 " + read + "；缓存写入 " + write,
      h("span", { class: "usage-tokens" }, [
        datum("arrowDown", r.i, "in"),
        datum("arrowUp", r.o, "out"),
        datum("databaseRead", r.cacheRead, "cr", true),
        datum("databaseWrite", r.cacheWrite, "cw", true),
      ]), { class: "usage-token-hint", tip: [tipRow("输入", nf(r.i), CT.in), tipRow("输出", nf(r.o), CT.out),
        tipRow("缓存读取", read, CT.cr),
        tipRow("缓存写入", write, CT.cw),
        r.cacheWrite30m == null ? null : tipRow("写入 · 30 分钟", nf(r.cacheWrite30m), CT.cw),
        r.cacheWrite12h == null ? null : tipRow("写入 · 12 小时", nf(r.cacheWrite12h), CT.cw),
        r.cacheWriteUnclassified ? tipRow("写入 · 未分档", nf(r.cacheWriteUnclassified), CT.cw) : null,
        r.cacheWriteTTLValid === false ? h("div", { class: "usage-tip-note" },
          "上游写入分档不完整或与合计不符，按合计和基础写入价结算。") : null,
        r.ctx == null ? null : tipRow("判定 ctx", nf(r.ctx) +
          (r.thr ? " / " + nf(r.thr) : ""), CT.ctx),
        tipRow("计量来源", r.tokenSource || "—"),
        tipRow("合计" + (r.cache == null ? "（不含未知缓存）" : ""),
          nf(r.i + r.o + (r.cache || 0)), undefined, true),
        r.cacheSplit ? null : h("div", { class: "usage-tip-note" },
          "上游未报告或旧记录未保留的缓存读写量显示为 —，不由缓存命中量推算写入量。"),
      ] });
  };
  const usageCost = (r) => {
    const rows = [tipRow("策略 / 模式", (POLICY[r.policy] || r.policy || "—") +
      " / " + (MODE[r.mode] || r.mode))];
    const price = (v) => v == null ? "—" : usd(Number(v));
    if (r.px) {
      rows.push(tipRow("输入价", price(r.px.i) + " /M", CT.in),
        tipRow("输出价", price(r.px.o) + " /M", CT.out));
      if (r.px.cr != null) rows.push(tipRow("缓存读价", price(r.px.cr) + " /M", CT.cr));
      if (r.px.cw != null) rows.push(tipRow("缓存写价", price(r.px.cw) + " /M", CT.cw));
      if (r.px.cw30 != null) rows.push(tipRow("写入 · 30 分钟价", price(r.px.cw30) + " /M", CT.cw));
      if (r.px.cw12 != null) rows.push(tipRow("写入 · 12 小时价", price(r.px.cw12) + " /M", CT.cw));
      rows.push(tipRow("档位", r.tier === "long" ? "长上下文" : "标准"));
    } else if (r.perReq) {
      rows.push(tipRow("按次单价", price(r.perPrice)), tipRow("次数", nf(r.units || 1)));
    } else rows.push(tipRow("定价快照", "未记录"));
    if (r.ratio != null) rows.push(tipRow("分组倍率", r.ratio + "x"));
    rows.push(tipRow("原价", usd(r.list)), tipRow("实际扣费", usd(r.cost), CT.cost, true));
    return usageHint("实际扣费 " + usd(r.cost) + "，查看定价明细",
      h("span", { class: "usage-cost" }, usd(r.cost)), { tip: rows });
  };

  /* 用户记录与全站日志只在客户列上不同，其余展示共用；筛选/分页仍由各页面负责。 */
  function usageColumns(onDetail, onUser) {
    return [
      { title: "时间", key: "t", width: 152, render: (r) =>
        h("span", { class: "usage-date" }, absTime(r.t)) },
      { title: "IP", key: "ip", width: 160, render: (r) =>
        usageHint("请求 IP：" + (r.ip || "未记录"), h("span", {
          class: "usage-ellipsis usage-date" }, r.ip || "—"), { class: "usage-identity" }) },
      ...(onUser ? [{ title: "客户", key: "email", width: 176, render: (r) =>
        usageHint(r.email + " · 点击筛选该用户", h("span", {
          class: "usage-ellipsis" }, r.email), { activate: () => onUser(r), class: "usage-identity" }) }] : []),
      { title: "API 密钥", key: "key", width: 116, render: (r) =>
        usageHint("API 密钥：" + r.key + (r.keyId ? " (#" + r.keyId + ")" : ""), [
          ic("key", 14), h("span", { class: "usage-ellipsis" }, r.key),
        ], { class: "usage-identity" }) },
      { title: "模型", key: "model", minWidth: 200, render: (r) =>
        h("div", { class: "usage-model" }, [
          usageHint(r.model, [
            provIcon(r.model, 16), h("span", { class: "usage-ellipsis" }, r.model),
          ], { class: "usage-identity" }),
          usageHint(r.stream ? "流式响应" : "非流式响应", ic(r.stream ? "stream" : "response", 15),
            { class: "usage-icon usage-secondary" }),
        ]) },
      { title: "计费", key: "mode", width: 76, align: "center", render: (r) =>
        usageHint((POLICY[r.policy] || r.policy || "未记录策略") + " · " +
          (MODE[r.mode] || r.mode) + (r.tier === "long" ? " · 长上下文" : ""),
        ic(r.free ? "gift" : r.policy === "quota" ? "gauge" :
          r.policy === "balance" ? "wallet" : r.perReq ? "tag" : "coins", 17),
        { class: "usage-icon" }) },
      { title: "Tokens", key: "i", width: 236, render: usageTokens },
      { title: "结果", key: "end", width: 76, align: "center", render: (r) =>
        usageHint((r.ok ? "成功" : "未正常结束") + " · " + (END_TEXT[r.end] || r.end || "正常结束"),
          ic(r.ok ? "circleCheck" : "circleX", 19, sem(r.ok ? "success" : "error")),
          { class: "usage-icon" }) },
      { title: "扣费", key: "cost", width: 112, render: usageCost },
      { title: "耗时", key: "ms", width: 116, render: usageTime },
      { title: "详情", key: "detail", width: 56, align: "center", render: (r) =>
        usageHint("查看请求详情", ic("eye", 17), {
          class: "usage-icon usage-action", activate: () => onDetail(r),
        }) },
    ];
  }

  const EXPIRY_PRESETS = [["永不过期", 0], ["1 小时", 3600], ["1 天", 86400],
    ["7 天", 7 * 86400], ["30 天", 30 * 86400], ["1 年", 365 * 86400]];
  /* CC Switch 一键导入:本机客户端注册的 ccswitch:// 协议,由客户端自行解析。 */
  const CCSWITCH_APPS = {
    claude: { label: "Claude", defaultName: "My Claude", fields: [
      ["model", "主模型", true], ["haikuModel", "Haiku 模型", false],
      ["sonnetModel", "Sonnet 模型", false], ["opusModel", "Opus 模型", false]] },
    codex: { label: "Codex", defaultName: "My Codex",
      fields: [["model", "主模型", true]] },
    gemini: { label: "Gemini", defaultName: "My Gemini",
      fields: [["model", "主模型", true]] },
  };
  const ccServerAddress = () => (location.origin + BASE).replace(/\/+$/, "");
  function buildCCSwitchURL(app, name, models, apiKey) {
    const server = ccServerAddress();
    const p = new URLSearchParams();
    p.set("resource", "provider");
    p.set("app", app);
    p.set("name", name);
    p.set("endpoint", app === "codex" ? server + "/v1" : server);
    p.set("apiKey", apiKey.indexOf("sk-") === 0 ? apiKey : "sk-" + apiKey);
    Object.keys(models).forEach((k) => { if (models[k]) p.set(k, models[k]); });
    p.set("homepage", server);
    p.set("enabled", "true");
    return "ccswitch://v1/import?" + p.toString();
  }
  const _ccModels = {};
  /* 用密钥本身当 bearer 打 /v1/models,拿到的就是这把钥匙真能用的模型。 */
  function ccLoadModels(apiKey) {
    if (_ccModels[apiKey]) return Promise.resolve(_ccModels[apiKey]);
    return fetch(BASE + "/v1/models",
      { headers: { Authorization: "Bearer " + apiKey } })
      .then((r) => (r.ok ? r.json() : { data: [] }))
      .then((j) => {
        const list = ((j && j.data) || []).map((m) => m.id).filter(Boolean);
        _ccModels[apiKey] = list;
        return list;
      })
      .catch(() => []);
  }

  /* ---- 图表:纯 div / 内联 SVG,不引图表库 ----
     柱状图用 div 高度百分比 + flex 均分:天然跟随容器宽度,不用算 viewBox,
     也不会因 preserveAspectRatio 把描边拉扁。SVG 只用在环形图(尺寸固定)。 */
  const BarChart = {
    props: ["items", "color", "height", "unit"],
    setup(props) {
      const max = computed(() => Math.max.apply(null,
        [0].concat((props.items || []).map((x) => x.value || 0))) || 1);
      return { max: max };
    },
    render() {
      const h0 = this.height || 108;
      const items = this.items || [];
      const bars = items.map((x) => {
        const v = x.value || 0;
        /* 有值就至少给 2px:0.3% 的柱子渲染成 0 高度,看着像那天没数据,
           而「没数据」和「量很小」是两件事。 */
        const pct = v > 0 ? Math.max((v / this.max) * 100, 2) : 0;
        const bar = h("div", { class: "console-chart-column", tabindex: 0,
          role: "img", "aria-label": x.label + ": " + (x.text != null ? x.text : nf(v)),
          style: { height: h0 + "px" } }, [
          h("div", { class: "console-chart-bar", style: { height: pct + "%",
            background: v > 0 ? this.color : "rgba(127,127,127,.13)",
            minHeight: "2px" } }),
        ]);
        return wrapTip(bar, [tipRow(x.label, x.text != null ? x.text : nf(v))], "top");
      });
      /* X 轴只标首末与中点:14 个日期标签在 300px 宽里必然叠字。 */
      const n = items.length;
      const ticks = n ? [0, Math.floor((n - 1) / 2), n - 1]
        .filter((i, k, a) => a.indexOf(i) === k) : [];
      return h("div", { class: "console-chart" }, [
        h("div", { class: "console-chart-plot" }, bars),
        h("div", { class: "console-chart-ticks" },
          ticks.map((i) => h("span", null, items[i].label))),
      ]);
    },
  };

  /* 环形图:一圈 circle 用 stroke-dasharray 拼扇区。尺寸固定,不参与自适应,
     所以可以放心用 SVG 而不必处理 viewBox 缩放。

     图本身不接指针事件:每个扇区都是一整个 circle 元素,后画的那个会盖住整圈,
     hover 永远只命中最后一段(实测 Playwright 报 intercepts pointer events)。
     明细由旁边的图例承载 —— 图例是文字行,悬浮区域清晰且可读屏。 */
  const Donut = {
    props: ["items", "size", "thickness", "center", "sub"],
    render() {
      const size = this.size || 132;
      const th = this.thickness || 13;
      const r = (size - th) / 2;
      const cir = 2 * Math.PI * r;
      const items = (this.items || []).filter((x) => (x.value || 0) > 0);
      const total = items.reduce((a, x) => a + (x.value || 0), 0);
      let acc = 0;
      const arcs = items.map((x) => {
        const frac = x.value / total;
        const node = h("circle", { cx: size / 2, cy: size / 2, r: r,
          fill: "none", stroke: x.color, "stroke-width": th,
          "stroke-dasharray": (frac * cir) + " " + cir,
          "stroke-dashoffset": -acc * cir,
          /* 旋转到 12 点起画:SVG 的 0 度在 3 点,直接画会让第一段从右侧开始。 */
          transform: "rotate(-90 " + (size / 2) + " " + (size / 2) + ")" });
        acc += frac;
        return node;
      });
      return h("div", { style: { position: "relative", width: size + "px",
        height: size + "px", flex: "0 0 auto" } }, [
        h("svg", { width: size, height: size,
          style: { pointerEvents: "none" } },
          [h("circle", { cx: size / 2, cy: size / 2, r: r, fill: "none",
            stroke: "rgba(127,127,127,.12)", "stroke-width": th })]
            .concat(arcs)),
        h("div", { style: { position: "absolute", inset: 0, display: "grid",
          placeItems: "center", textAlign: "center", pointerEvents: "none" } },
          h("div", null, [
            h("div", { class: "console-donut-value" }, h(MetricValue, { value: this.center })),
            this.sub == null ? null : h("div", { class: "console-donut-label" }, this.sub),
          ])),
      ]);
    },
  };

  /* 头像:有图用图,没图用邮箱首字母 + 主题渐变。size 与圆角都可调,
     顶栏用小圆、资料页用大圆角方块(参考图形态)。 */
  const Avatar = {
    props: ["src", "text", "size", "radius", "font"],
    render() {
      const sz = this.size || 40;
      const st = { width: sz + "px", height: sz + "px", flex: "0 0 auto",
        borderRadius: this.radius || "50%", overflow: "hidden",
        display: "grid", placeItems: "center" };
      if (this.src)
        return h("img", { src: this.src, alt: "",
          style: Object.assign({ objectFit: "cover" }, st) });
      return h("span", { style: Object.assign({
        background: "#a8613c", color: "#fff",
        fontWeight: 700, lineHeight: 1,
        fontSize: (this.font || Math.round(sz * 0.42)) + "px" }, st) },
        String(this.text || "?").slice(0, 1).toUpperCase());
    },
  };

  /* 前端压缩头像:canvas 缩到 256px 内,再按质量二分逼近目标字节数。
     压不到就拒绝,不静默上传超大图 —— data URI 是要塞进 /api/me 响应的。
     GIF 不能压(canvas 会丢掉动画),只校验大小后原样返回。 */
  const AVATAR_MAX = 20 * 1024;
  function shrinkAvatar(file) {
    return new Promise(function (resolve, reject) {
      if (!/^image\//.test(file.type))
        return reject(new Error("请选择图片文件"));
      const fr = new FileReader();
      fr.onerror = () => reject(new Error("读取文件失败"));
      fr.onload = () => {
        const raw = String(fr.result);
        if (file.type === "image/gif") {
          if (raw.length > 28000)
            return reject(new Error("GIF 不做压缩,请自行控制在 20KB 以内"));
          return resolve(raw);
        }
        const img = new Image();
        img.onerror = () => reject(new Error("图片解析失败"));
        img.onload = () => {
          const side = Math.min(img.width, img.height);
          const cv = document.createElement("canvas");
          cv.width = cv.height = Math.min(side, 256);
          const cx = cv.getContext("2d");
          /* 居中裁成正方形:头像容器是方的,直接缩放会把人脸压扁。 */
          cx.drawImage(img, (img.width - side) / 2, (img.height - side) / 2,
            side, side, 0, 0, cv.width, cv.height);
          let q = 0.9;
          let out = cv.toDataURL("image/webp", q);
          /* webp 不被支持时 toDataURL 会静默回落成 png,那就改走 jpeg。 */
          const type = out.indexOf("data:image/webp") === 0 ? "image/webp"
            : "image/jpeg";
          for (let i = 0; i < 8 && out.length > 28000; i++) {
            q -= 0.1;
            out = cv.toDataURL(type, Math.max(q, 0.2));
          }
          if (out.length > 28000)
            return reject(new Error("图片过大,压缩后仍超过 20KB"));
          resolve(out);
        };
        img.src = raw;
      };
      fr.readAsDataURL(file);
    });
  }

  const go = (hash) => { location.hash = hash; };

  const ADMIN_NAV = [
    { key: "stats", label: "运行概览", icon: "grid", group: "运行管理", description: "请求趋势、消费与上游资源的运行视图" },
    { key: "logs", label: "调用记录", icon: "doc", group: "运行管理", description: "从请求结果定位模型、用户与计费明细" },
    { key: "channels", label: "渠道与资源", icon: "network", group: "运行管理", description: "连接上游渠道,检查配置与资源池" },
    { key: "pool", label: "号池管理", icon: "database", group: "运行管理", description: "逐号查看状态、用量与到期时间,批量启停、测试与删除" },
    { key: "users", label: "用户管理", icon: "user", group: "访问与计费", description: "查找用户,管理分组、状态与账户余额" },
    { key: "groups", label: "用户分组", icon: "users", group: "访问与计费", description: "定义模型范围、访问限制与计费策略" },
    { key: "pricing", label: "模型定价", icon: "dollar", group: "访问与计费", description: "维护模型费率与上下文计价档位" },
    { key: "announcements", label: "站点公告", icon: "bell", group: "站点运营", description: "编辑公告内容与通知方式" },
    { key: "invites", label: "注册邀请码", icon: "ticket", group: "站点运营", description: "发放与管理注册资格" },
    { key: "codes", label: "兑换码与订单", icon: "gift", group: "站点运营", description: "生成余额兑换码，核对充值订单与到账情况" },
    { key: "settings", label: "站点设置", icon: "gear", group: "站点运营", description: "分区管理注册、安全、邮件与计费设置" },
  ];

  const ModelCachePanel = {
    props: { scope: { type: String, default: "user" }, defaultRange: { type: String, default: "all" } },
    setup(props) {
      const range = ref(props.defaultRange), search = ref(""), data = ref(null), page = ref(1);
      const loading = ref(false), error = ref("");
      const trendModels = ref([]), trend = ref(null), trendError = ref(""), trendLoading = ref(false);
      const activeDate = ref(null), trendRoot = ref(null);
      const chartWidth = ref(960);
      let chartObserver;
      let sequence = 0, trendSequence = 0;
      const label = props.scope === "site" ? "全站" : "个人";
      const load = async () => {
        const current = ++sequence;
        loading.value = true; error.value = ""; data.value = null; page.value = 1;
        try {
          const path = props.scope === "site" ? "/admin/usage/cache" : "/usage/cache";
          const result = await api("GET", path + "?range=" + range.value, undefined, { timeout: 20000 });
          if (current === sequence) data.value = result;
        } catch (e) { if (current === sequence) error.value = e.message; }
        finally { if (current === sequence) loading.value = false; }
      };
      onMounted(load);
      onMounted(() => {
        chartObserver = new ResizeObserver(entries => {
          chartWidth.value = Math.max(360, Math.round(entries[0].contentRect.width));
        });
        chartObserver.observe(trendRoot.value);
      });
      watch(range, load);
      watch([data, trendModels], loadTrend);
      onUnmounted(() => { sequence++; trendSequence++; chartObserver?.disconnect(); });
      const models = computed(() => (data.value?.models || []).filter(row =>
        !search.value.trim() || String(row.model || "").toLowerCase().includes(search.value.trim().toLowerCase())));
      watch(search, () => { page.value = 1; });
      const summary = computed(() => {
        if (!data.value) return null;
        if (!search.value.trim()) return data.value.summary;
        const requests = models.value.reduce((n,row) => n + row.requests, 0);
        const totalInput = models.value.reduce((n,row) => n + row.total_input_tokens, 0);
        const known = models.value.filter(row => row.cache_tokens != null);
        const cached = known.reduce((n,row) => n + row.cache_tokens, 0);
        return { requests, cache_rate: known.length && totalInput ? cached / totalInput * 100 : null };
      });
      const pagination = computed(() => ({ page: page.value, pageSize: 10,
        onChange: value => { page.value = value; } }));
      const rate = row => row?.cache_rate == null ? "—" : fmt(row.cache_rate, 1) + "%";
      const count = (value) => value == null ? "未提供" : nf(value);
      const tokens = (row, field, coverage) => h("span", {
        class: "model-cache-number", title: "缓存数据覆盖 " + row[coverage] + " / " + row.requests + " 次请求",
      }, count(row[field]) + (row[field] != null && row[coverage] < row.requests ? " *" : ""));
      async function loadTrend() {
        const current = ++trendSequence;
        trend.value = null; trendError.value = ""; activeDate.value = null;
        const snapshot = data.value;
        if (!snapshot) { trendLoading.value = false; return; }
        trendLoading.value = true;
        try {
          const base = props.scope === "site" ? "/admin/usage/cache/trend" : "/usage/cache/trend";
          const query = "?range=" + snapshot.range + "&as_of=" + snapshot.as_of +
            trendModels.value.map(model => "&models=" + encodeURIComponent(model)).join("");
          const result = await api("GET", base + query, undefined, { timeout: 20000 });
          if (current === trendSequence) trend.value = result;
        } catch (e) { if (current === trendSequence) trendError.value = e.message; }
        finally { if (current === trendSequence) trendLoading.value = false; }
      }
      const trendTime = seconds => new Date(seconds * 1000).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
      const trendOptions = computed(() => {
        const options = (data.value?.models || []).map(row => ({ label: row.model, value: row.model }));
        for (const model of trendModels.value)
          if (!options.some(option => option.value === model)) options.push({ label: model, value: model });
        return options;
      });
      const trendCaption = computed(() => trendModels.value.length ? "对比 " + trendModels.value.length + " 个模型" : "全部模型汇总");
      const metricDefs = [
        { key: "input_tokens", label: "Input", color: "#3974e6" },
        { key: "output_tokens", label: "Output", color: "#009e78" },
        { key: "cache_write_tokens", label: "Cache Creation", color: "#bd7500", coverage: "cache_write_rows" },
        { key: "cache_read_tokens", label: "Cache Read", color: "#009cb5", coverage: "cache_read_rows" },
        { key: "cache_hit_rate", label: "Cache Hit Rate", color: "#8b4de6", coverage: "cache_read_rows", percent: true },
      ];
      const hiddenMetrics = ref([]);
      const toggleMetric = key => {
        hiddenMetrics.value = hiddenMetrics.value.includes(key)
          ? hiddenMetrics.value.filter(item => item !== key) : [...hiddenMetrics.value, key];
      };
      const seriesStyles = new Map();
      const dashFor = model => {
        if (model == null) return "";
        if (!seriesStyles.has(model)) seriesStyles.set(model, seriesStyles.size);
        return ["", "8 4", "2 3", "10 3 2 3", "4 3", "12 5"][seriesStyles.get(model) % 6];
      };
      const chartSeries = computed(() => {
        if (!trend.value) return [];
        const series = trend.value.model_series || [{ model: null, series: trend.value.series, summary: trend.value.summary }];
        return series.map(item => ({ ...item, dash: dashFor(item.model), label: item.model ?? "全部模型汇总",
          byDate: new Map(item.series.map(row => [row.date, row])) }));
      });
      const trendRows = computed(() => trend.value?.series || []);
      const focusedDay = computed(() => trendRows.value.find(row => row.date === activeDate.value) ||
        trendRows.value[trendRows.value.length - 1] || null);
      const focusedModels = computed(() => chartSeries.value.map(item => ({ ...item, row: item.byDate.get(focusedDay.value?.date) })));
      const moveDay = delta => {
        const rows = trendRows.value;
        if (!rows.length) return;
        const index = rows.findIndex(row => row.date === focusedDay.value?.date);
        activeDate.value = rows[Math.max(0, Math.min(rows.length - 1, index + delta))].date;
      };
      const metricValue = (row, metric) => {
        if (!row?.requests) return null;
        if (metric.percent) return row.cache_read_tokens == null || row.cache_tokens == null || !row.total_input_tokens
          || row.cache_read_tokens > row.total_input_tokens
          ? null : row.cache_read_tokens / row.total_input_tokens * 100;
        return row[metric.key] ?? null;
      };
      const metricText = (row, metric, full = false) => {
        const value = metricValue(row, metric);
        if (value == null) return "未提供";
        const text = metric.percent ? fmt(value, 1) + "%" : full ? nf(value) : kf(value);
        return text + ((metric.coverage && row[metric.coverage] < row.requests || metric.percent && row.cache_rows < row.requests) ? " *" : "");
      };
      const money = value => value == null ? "未提供" : usd(value);
      const dayTip = row => [row.date, ...chartSeries.value.map(item => {
        const daily = item.byDate.get(row.date);
        return item.label + "：" + (daily?.requests
          ? metricDefs.map(metric => metric.label + " " + metricText(daily, metric, true)).join("；") +
            "；Actual " + money(daily.actual_cost) + "；Standard " + money(daily.cost) : "当天无请求");
      })].join("\n");
      const plot = computed(() => {
        const rows = trendRows.value, items = [], ticks = [];
        const width = chartWidth.value, left = 64, right = width - 64, top = 22, bottom = 220;
        const step = rows.length > 1 ? (right-left) / (rows.length - 1) : right-left;
        const tickStep = Math.max(1, Math.ceil((rows.length - 1) / Math.max(2, Math.floor((right-left) / 120))));
        rows.forEach((row, index) => {
          const x = left + index * step;
          items.push({ row, x, hitX: Math.max(left, x-step/2),
            hitWidth: rows.length === 1 ? right-left : Math.min(right, x+step/2)-Math.max(left, x-step/2) });
          if (index % tickStep === 0 || index === rows.length - 1)
            ticks.push({ x, text: row.date, anchor: index === 0 ? "start" : index === rows.length-1 ? "end" : "middle" });
        });
        const visible = metricDefs.filter(metric => !hiddenMetrics.value.includes(metric.key));
        let maximum = 0;
        for (const item of chartSeries.value) for (const row of item.series) for (const metric of visible)
          if (!metric.percent) maximum = Math.max(maximum, metricValue(row, metric) ?? 0);
        const rawStep = Math.max(1, maximum / 6), magnitude = 10 ** Math.floor(Math.log10(rawStep));
        const unit = ([1, 2, 2.5, 5, 10].find(value => value >= rawStep / magnitude) || 10) * magnitude;
        const tokenMax = Math.max(6, unit * 6);
        const lines = chartSeries.value.flatMap(item => visible.map(metric => {
          const paths = [], points = [];
          let segment = [];
          items.forEach(point => {
            const row = item.byDate.get(point.row.date), value = metricValue(row, metric);
            const y = value == null ? null : bottom - value / (metric.percent ? 100 : tokenMax) * (bottom-top);
            if (y == null) { if (segment.length) paths.push(segment.join(" ")); segment = []; }
            else { segment.push((segment.length ? "L" : "M") + point.x + "," + y); points.push({ date: point.row.date, x: point.x, y }); }
          });
          if (segment.length) paths.push(segment.join(" "));
          return { key: String(item.model) + ":" + metric.key, color: metric.color, dash: item.dash, paths, points };
        }));
        return { items, lines, hasPoints: lines.some(line => line.points.length), ticks, width, left, right, top, bottom,
          tokenTicks: Array.from({ length: 7 }, (_, index) => ({ value: unit * index, y: bottom - index / 6 * (bottom-top) })),
          rateTicks: [0,20,40,60,80,100].map(value => ({ value, y: bottom - value / 100 * (bottom-top) })) };
      });
      const focusedPoint = computed(() => plot.value.items.find(point => point.row.date === focusedDay.value?.date));
      const tooltipStyle = computed(() => ({
        left: "clamp(8px, " + ((focusedPoint.value?.x || 64) / plot.value.width * 100) + "%, calc(100% - 268px))",
      }));
      const selectTrend = row => {
        if (!trendModels.value.includes(row.model)) trendModels.value = [...trendModels.value, row.model];
        trendRoot.value?.scrollIntoView({ block: "start" });
      };
      const columns = [
        { title: "模型", key: "model", minWidth: 210, render: row => h("button", {
            type: "button", class: "model-cache-model-button", title: "加入 Token 使用趋势对比",
            "aria-label": "查看 " + row.model + " 的 Token 使用趋势", onClick: () => selectTrend(row),
          }, [modelChip(row.model)]) },
        { title: "请求", key: "requests", width: 80, align: "right", sorter: (a,b) => a.requests-b.requests,
          render: row => nf(row.requests) },
        { title: "总输入 Token", key: "total_input_tokens", width: 125, align: "right",
          render: row => h("span", { class: "model-cache-number" }, nf(row.total_input_tokens)) },
        { title: "缓存 Token", key: "cache_tokens", width: 125, align: "right",
          sorter: (a,b) => (a.cache_tokens ?? -1)-(b.cache_tokens ?? -1),
          render: row => tokens(row, "cache_tokens", "cache_rows") },
        { title: "缓存读取", key: "cache_read_tokens", width: 125, align: "right",
          render: row => tokens(row, "cache_read_tokens", "cache_read_rows") },
        { title: "缓存写入", key: "cache_write_tokens", width: 125, align: "right",
          render: row => tokens(row, "cache_write_tokens", "cache_write_rows") },
        { title: "缓存率 / 输入构成", key: "cache_rate", width: 210,
          sorter: (a,b) => (a.cache_rate ?? -1)-(b.cache_rate ?? -1),
          render: row => h("div", { class: "model-cache-rate" }, [
            h("strong", null, rate(row)),
            h("div", { class: "model-cache-bar", role: "img",
              "aria-label": "缓存率 " + rate(row), title: "缓存 ÷（未缓存输入 + 缓存），不含输出" },
              row.cache_rate == null ? [] : [
                h("span", { class: "model-cache-uncached", style: { width: (100-row.cache_rate) + "%" } }),
                h("span", { class: "model-cache-cached", style: { width: row.cache_rate + "%" } }),
              ]),
            h("small", null, row.cache_rows === row.requests ? "全部记录" : row.cache_rows + "/" + row.requests + " 条缓存可算"),
          ]) },
      ];
      return { range, search, models, summary, loading, error, label, load, columns, rate, nf, kf,
        trendModels, trend, trendError, trendLoading, trendRoot, trendOptions, trendCaption, chartSeries, focusedModels, focusedDay, focusedPoint,
        activeDate, plot, moveDay, dayTip, loadTrend, trendTime, pagination,
        metricDefs, hiddenMetrics, toggleMetric, metricText, tooltipStyle, money };
    },
    template: `
<n-card class="model-cache-panel console-table-card" size="small" title="模型缓存使用">
  <template #header-extra>
    <n-space :size="8" align="center" wrap>
      <n-radio-group v-model:value="range" size="small" aria-label="缓存统计时间范围">
        <n-radio-button value="day">近 24 小时</n-radio-button><n-radio-button value="week">7 天</n-radio-button>
        <n-radio-button value="month">30 天</n-radio-button><n-radio-button value="all">累计</n-radio-button>
      </n-radio-group>
      <n-button size="small" secondary :loading="loading" @click="load">刷新</n-button>
    </n-space>
  </template>
  <p class="console-section-note">{{ label }}已调用模型 · 缓存率 = 缓存读写合计 ÷ 总输入，不含输出。缺失字段显示“未提供”，* 表示仅有部分记录。</p>
  <section ref="trendRoot" class="model-cache-trend">
    <div class="model-cache-trend-heading">
      <div><h3>Token 使用趋势</h3><p class="console-section-note">北京时间 · {{ trendCaption }}<template v-if="trend"> · 截至 {{ trendTime(trend.as_of) }}</template></p></div>
      <n-select v-model:value="trendModels" class="model-cache-trend-select" :options="trendOptions" multiple :max-tag-count="2"
        filterable clearable :disabled="loading" placeholder="选择模型进行对比（默认全部汇总）" aria-label="多选 Token 使用趋势模型"/>
    </div>
    <div v-if="trendLoading" class="model-cache-trend-loading"><n-spin size="small"/>正在读取按天趋势</div>
    <n-alert v-else-if="trendError" type="error" :show-icon="false">{{ trendError }} <n-button text @click="loadTrend">重试趋势</n-button></n-alert>
    <template v-else-if="trend && trend.summary.requests">
      <div class="model-cache-trend-legend" aria-label="Token 使用趋势图例">
        <button v-for="metric in metricDefs" :key="metric.key" type="button" :style="{'--metric-color':metric.color}"
          :aria-pressed="!hiddenMetrics.includes(metric.key)" :class="{'is-hidden':hiddenMetrics.includes(metric.key)}" @click="toggleMetric(metric.key)">
          <i></i>{{ metric.label }}
        </button>
      </div>
      <div v-if="chartSeries.length>1" class="model-cache-trend-models" aria-label="对比模型线型">
        <span v-for="line in chartSeries" :key="line.model">
          <svg width="24" height="10" aria-hidden="true"><line x1="0" x2="24" y1="5" y2="5" stroke="currentColor" :stroke-dasharray="line.dash" stroke-width="2"/></svg>{{ line.label }}
        </span>
      </div>
      <div v-if="plot.hasPoints" class="model-cache-trend-scroll">
        <div class="model-cache-trend-canvas" @mouseleave="activeDate=null">
          <svg :viewBox="'0 0 '+plot.width+' 264'" class="model-cache-trend-svg" role="img" tabindex="0"
            :aria-label="label + trendCaption + 'Token 使用趋势，左轴 Token，右轴命中率；左右方向键逐日查看。' + (focusedDay ? dayTip(focusedDay) : '')"
            @focus="activeDate=focusedDay?.date" @blur="activeDate=null" @keydown.esc="activeDate=null"
            @keydown.left.prevent="moveDay(-1)" @keydown.right.prevent="moveDay(1)">
            <g v-for="tick in plot.tokenTicks" :key="tick.value">
              <line :x1="plot.left" :x2="plot.right" :y1="tick.y" :y2="tick.y" class="model-cache-trend-grid"/>
              <text x="54" :y="tick.y+4" text-anchor="end" class="model-cache-trend-axis">{{ kf(tick.value) }}</text>
            </g>
              <text v-for="tick in plot.rateTicks" :key="tick.value" :x="plot.right+12" :y="tick.y+4" class="model-cache-trend-axis model-cache-trend-rate-axis">{{ tick.value }}%</text>
            <g v-for="line in plot.lines" :key="line.key">
              <path v-for="(path,index) in line.paths" :key="index" :d="path" class="model-cache-trend-line" :stroke="line.color" :stroke-dasharray="line.dash"/>
              <circle v-for="point in line.points" :key="point.date" :cx="point.x" :cy="point.y" r="3.5" class="model-cache-trend-dot" :stroke="line.color"/>
            </g>
            <line v-if="activeDate && focusedPoint" :x1="focusedPoint.x" :x2="focusedPoint.x" :y1="plot.top" :y2="plot.bottom" class="model-cache-trend-cursor"/>
            <rect v-for="point in plot.items" :key="point.row.date" :x="point.hitX" :y="plot.top" :width="point.hitWidth" :height="plot.bottom-plot.top" fill="transparent"
              :aria-label="dayTip(point.row)" @mouseenter="activeDate=point.row.date" @click="activeDate=point.row.date"/>
            <text v-for="tick in plot.ticks" :key="tick.text" :x="tick.x" y="248" :text-anchor="tick.anchor" class="model-cache-trend-axis">{{ tick.text }}</text>
          </svg>
          <div v-if="activeDate && focusedDay" class="model-cache-trend-tooltip" :style="tooltipStyle" role="status" aria-live="polite">
            <strong class="model-cache-tip-date">{{ focusedDay.date }}</strong>
            <div v-for="item in focusedModels" :key="item.model ?? 'all'" class="model-cache-tip-group">
              <strong v-if="trendModels.length" class="model-cache-tip-model">{{ item.label }}</strong>
              <template v-if="item.row?.requests">
                <div v-for="metric in metricDefs" :key="metric.key" class="model-cache-tip-row">
                  <span><i :style="{background:metric.color}"></i>{{ metric.label }}</span><b>{{ metricText(item.row,metric) }}</b>
                </div>
                <div class="model-cache-tip-cost">Actual: {{ money(item.row.actual_cost) }} | Standard: {{ money(item.row.cost) }}</div>
              </template><span v-else>当天无请求</span>
            </div>
          </div>
        </div>
      </div>
      <n-empty v-else description="暂无已启用的指标，请点击图例显示" size="small"/>
    </template>
    <n-empty v-else-if="trend && !loading" description="所选模型在此范围内没有调用记录" size="small"/>
    <p class="console-section-note model-cache-trend-note">左轴为 Token，右轴为命中率（缓存读取 ÷ 总输入）。可点击图例开关指标，多选模型按不同线型对比；无请求或未提供的指标留空。悬停、点选或使用左右方向键查看当天用量与费用。</p>
  </section>
  <div class="model-cache-toolbar">
    <n-input v-model:value="search" clearable size="small" class="model-cache-search" placeholder="搜索模型" aria-label="搜索缓存统计模型"/>
    <div class="model-cache-legend"><span><i class="model-cache-uncached"></i>未缓存输入</span><span><i class="model-cache-cached"></i>缓存输入</span></div>
    <span v-if="summary" class="console-section-note">共 {{ nf(models.length) }} 个模型 · {{ nf(summary.requests) }} 次请求 · 缓存率 {{ rate(summary) }}</span>
  </div>
  <n-alert v-if="error" type="error" :show-icon="false">{{ error }} <n-button text @click="load">重试</n-button></n-alert>
  <n-data-table v-else :loading="loading" :columns="columns" :data="models" size="small"
    :bordered="false" :scroll-x="1100" :pagination="pagination" :row-key="r=>r.model || ''"/>
</n-card>`,
  };

  const LiveUsageMetrics = {
    props: { scope: { type: String, default: "user" } },
    setup(props) {
      const label = props.scope === "site" ? "全站" : "个人";
      const path = props.scope === "site" ? "/admin/usage/live" : "/usage/live";
      const stats = ref(null);
      const failed = ref(false);
      let loading = false, timer = null;
      const items = [
        { key: "today_cost", label: "用量", hint: label + "今日用量金额 · 北京时间 00:00 起 · 美元" },
        { key: "rpm", label: "RPM", hint: label + "最近 60 秒已记录请求数" },
        { key: "tpm", label: "TPM", hint: label + "最近 60 秒已记录 Token 数，含缓存" },
      ];
      const refresh = async () => {
        if (loading || document.hidden || !token()) return;
        loading = true;
        try {
          stats.value = await api("GET", path, undefined, { timeout: 10000 });
          failed.value = false;
        } catch (_) { failed.value = true; }
        finally { loading = false; }
      };
      onMounted(() => {
        refresh();
        timer = setInterval(refresh, 5000);
        document.addEventListener("visibilitychange", refresh);
      });
      onUnmounted(() => {
        clearInterval(timer);
        document.removeEventListener("visibilitychange", refresh);
      });
      const value = (item) => !stats.value || failed.value ? "—"
        : item.key === "today_cost" ? "$" + fmt(stats.value.today_cost, 4) : nf(stats.value[item.key]);
      const hint = (item) => failed.value ? label + "统计暂不可用，稍后自动刷新"
        : item.hint + " · 每 5 秒刷新";
      return { items, value, hint, label };
    },
    template: `
<div class="console-live-metrics" role="group" :aria-label="label + '实时用量'">
  <div v-for="item in items" :key="item.key" class="console-live-metric" :title="hint(item)">
    <span>{{ item.label }}</span><strong>{{ value(item) }}</strong>
  </div>
</div>`,
  };

  window.BitPortal = {
    LiveUsageMetrics: LiveUsageMetrics, ModelCachePanel: ModelCachePanel,
    ADMIN_NAV: ADMIN_NAV,
    BASE: BASE, api: api, qs: qs, download: download,
    token: token, setToken: setToken, clearToken: clearToken, authHeaders: authHeaders,
    themeOverrides: themeOverrides,
    dark: dark, theme: theme, go: go, narrow: narrow,
    msg: msg, dlg: dlg, copy: copy,
    nf: nf, usd: usd, kf: kf, fmt: fmt, MONO: MONO, SUBSTY: SUBSTY,
    absTime: absTime, dayTime: dayTime, relTime: relTime,
    planDur: planDur, planKind: planKind, planLeft: planLeft,
    ST: ST, st: st, POLICY: POLICY, MODE: MODE, tag: tag, money: money,
    ANN_LEVEL: ANN_LEVEL, ANN_MODE: ANN_MODE,
    REASON: REASON, reasonOf: reasonOf,
    PROV: PROV, prov: prov, provIcon: provIcon, ICONS: ICONS, ic: ic, Ic: Ic,
    C: C, CT: CT, tipRow: tipRow, wrapTip: wrapTip,
    subline: subline, stack: stack, box: box,
    modelChip: modelChip, costChip: costChip, sem: sem, timeRow: timeRow,
    pill: pill, Pills: Pills, StatCard: StatCard, MetricValue: MetricValue, PageTitle: PageTitle,
    BarChart: BarChart, Donut: Donut, Avatar: Avatar,
    shrinkAvatar: shrinkAvatar, AVATAR_MAX: AVATAR_MAX,
    elapsedLv: elapsedLv, frtLv: frtLv, tpsOf: tpsOf, kindOf: kindOf,
    normUsage: normUsage, END_TEXT: END_TEXT, EXPIRY_PRESETS: EXPIRY_PRESETS,
    usageColumns: usageColumns, usageHint: usageHint,
    CCSWITCH_APPS: CCSWITCH_APPS, ccServerAddress: ccServerAddress,
    buildCCSwitchURL: buildCCSwitchURL, ccLoadModels: ccLoadModels,
  };
})();
