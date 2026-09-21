/* bit-api 控制台共用层:主题、请求、格式化、图标与小组件。 */
(function () {
  "use strict";
  const { h, ref, computed, watchEffect } = Vue;

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

  const token = () => localStorage.getItem(TOKEN_KEY) || "";
  const setToken = (t) => localStorage.setItem(TOKEN_KEY, t);
  const clearToken = () => localStorage.removeItem(TOKEN_KEY);

  /* 401 一律视为登录态失效:清 token 并回登录页,避免各页面各写一遍。 */
  function api(method, path, body) {
    const opts = { method: method, headers: {} };
    if (token()) opts.headers["Authorization"] = "Bearer " + token();
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    return fetch(BASE + "/api" + path, opts).then((r) =>
      r.json().catch(() => ({})).then((j) => {
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
        return j;
      }));
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
    const opts = { headers: {} };
    if (token()) opts.headers["Authorization"] = "Bearer " + token();
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
      infoColor: "#4d7fa0", successColor: "#15803d",
      warningColor: "#a17a10", errorColor: "#b91c1c",
      borderRadius: "10px", borderRadiusSmall: "8px",
      fontFamily: '-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,' +
        '"PingFang SC","Microsoft YaHei",sans-serif',
      fontFamilyMono: "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace",
    },
    Card: { borderRadius: "14px" },
  };
  /* 深色主题共用同一个色相,只把亮度往上抬:低饱和色是按白底挑的,压在近黑底上
     对比度不够(#a8613c 约 3.5:1,选中态的小字会发灰)。不换色相 ——
     主按钮在两套主题下必须是同一个颜色,否则那是两个品牌。 */
  const overridesDark = {
    common: Object.assign({}, overrides.common, {
      primaryColor: "#d99a72", primaryColorHover: "#e5b092",
      primaryColorPressed: "#c98860", primaryColorSuppl: "#d1906a",
      infoColor: "#79a3c0", successColor: "#4d9960",
      warningColor: "#cfa53c", errorColor: "#d95a5a",
    }),
    Card: { borderRadius: "14px" },
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
    const done = () => msg.success((label || "已复制") + "");
    if (navigator.clipboard && location.protocol === "https:") {
      navigator.clipboard.writeText(text).then(done, () => fallback(text, done));
    } else fallback(text, done);
  }
  function fallback(text, done) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.cssText = "position:fixed;top:-9999px";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); done(); }
    catch (e) { msg.error("复制失败,请手动选中"); }
    document.body.removeChild(ta);
  }

  const nf = (n) => Number(n || 0).toLocaleString("en-US");
  const usd = (v) => ((v || 0) < 0 ? "-$" : "$") + Math.abs(v || 0).toFixed(4);
  const kf = (n) => (n >= 1000 ? (n / 1000).toFixed(1) + "K" : String(n || 0));
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
  const SEM = { success: ["#15803d", "#4d9960"], warning: ["#a17a10", "#cfa53c"],
    error: ["#b91c1c", "#d95a5a"] };
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
     单色图(openai/grok/inception 用 fill="currentColor")在 <img> 里会解析成黑,
     深色主题下要反色才看得见 —— 见 provIcon。
     顺序即优先级,第一个命中的胜出。 */
  const PROV = [
    [/gpt|chatgpt|^o[134]-/, "#10a37f", "OpenAI", "O", "openai", 1],
    /* Claude 系在别家渠道常被改名叫 fable-5 / opus-4.8 / sonnet-5 之类,
       名字里没有 claude,单靠 /claude/ 会全部漏成「自建」。 */
    [/claude|anthropic|fable|opus|sonnet/, "#d97757", "Anthropic", "A",
     "claude-color"],
    [/gemma/, "#4285f4", "Gemma", "G", "gemma-color"],
    [/gemini|learnlm/, "#4285f4", "Google", "G", "gemini-color"],
    /* veo / nano-banana 是 Google 的视频与图像模型,名字里不带 gemini。 */
    [/veo-|nano-banana/, "#4285f4", "Google", "G", "google-color"],
    /* xai 加词界:minimaxai/* 里含 "xai",不加会被认成 xAI 而抢在 minimax 前面。 */
    [/grok|\bxai\b/, "#71717a", "xAI", "X", "grok", 1],
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
    [/nvidia|nvcf/, "#76b900", "NVIDIA", "N", "nvidia-color"],
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
    arrowDown: "M12 4v14m0 0-5.5-5.5M12 18l5.5-5.5",
    arrowUp: "M12 20V6m0 0L6.5 11.5M12 6l5.5 5.5",
    cacheRead: "M4 6.5c0-1.4 3.6-2.5 8-2.5s8 1.1 8 2.5S16.4 9 12 9 4 7.9 4 6.5Z" +
      "m16 5.3c0 1.4-3.6 2.5-8 2.5s-8-1.1-8-2.5m16 5.2c0 1.4-3.6 2.5-8 2.5" +
      "s-8-1.1-8-2.5M4 6.5V17m16-10.5V17",
    cacheWrite: "M4.5 19.5h4L19 9a2.83 2.83 0 0 0-4-4L4.5 15.5v4Zm10-14 4 4",
    clock: "M12 7.2V12l3.4 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z",
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
    chevronUp: "m6.5 14.8 5.5-5.6 5.5 5.6",
    chevronDown: "m6.5 9.2 5.5 5.6 5.5-5.6",
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
    width: size || 13, height: size || 13, fill: "none", stroke: "currentColor",
    "stroke-width": sw || 1.9, "stroke-linecap": "round", "stroke-linejoin": "round",
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

  /* 顶部概览卡:柔和色底图标块 + 标签 + 大数 + 一行补充。
     value2 给「今日 / 累计」这种主副双值:副值同色系但降饱和,不抢主值。
     rows 给需要两行指标的卡(如 RPM / TPM),此时不显示 value。 */
  const StatCard = {
    props: ["label", "value", "value2", "sub", "sub2", "color", "icon", "tint",
      "rows"],
    render() {
      const big = (txt, color) => h("span", { style: Object.assign({
        fontSize: "21px", fontWeight: 700, lineHeight: 1.15,
        letterSpacing: "-.02em", color: color }, MONO) }, txt);
      let body;
      if (this.rows && this.rows.length) {
        body = h("div", { style: { display: "flex", flexDirection: "column",
          gap: "1px" } }, this.rows.map((r) => h("div", { style: {
            display: "flex", alignItems: "baseline", gap: "6px" } }, [
          h("span", { style: Object.assign({ fontSize: "18px", fontWeight: 700,
            lineHeight: 1.2, color: r[2] }, MONO) }, r[1]),
          h("span", { style: { fontSize: "11.5px", opacity: 0.55 } }, r[0]),
        ])));
      } else {
        body = h("div", { style: { display: "flex", alignItems: "baseline",
          gap: "5px", flexWrap: "wrap" } }, [
          h("span", { style: litUp() },
            [big(this.value, this.tint ? this.color : undefined)]),
          this.value2 == null ? null : h("span", { style: Object.assign({
            fontSize: "13px", fontWeight: 600, opacity: 0.5 }, MONO) },
            "/ " + this.value2),
        ]);
      }
      return h(naive.NCard, { size: "small", bordered: true,
        /* 等高:一行里性能指标卡有两行数字、Token 卡的补充文字要折两行,
           不锁 100% 会高低不齐。 */
        style: { height: "100%" },
        contentStyle: "padding:14px 15px" }, () => h("div", { style: {
          display: "flex", alignItems: "center", gap: "12px" } }, [
        /* 图标保留各卡片自己的色相(彩色淡底 + 同色描线):这一枚是卡片里唯一的
           视觉锚点,全灰之后八张卡扫过去就只剩标题在区分。饱和度已经在色值上
           压过一档,深色主题再用 litUp 把整枚提亮。 */
        h("span", { style: Object.assign({ display: "grid",
          placeItems: "center", width: "36px", height: "36px",
          borderRadius: "11px", flex: "0 0 auto",
          background: this.color + "1e" }, litUp()) },
          ic(this.icon, 18, this.color, 1.8)),
        h("div", { style: { minWidth: 0 } }, [
          h("div", { style: { fontSize: "12px", opacity: 0.6,
            marginBottom: "1px" } }, this.label),
          body,
          /* 补充文字允许折到第二行:三段 token 明细一行放不下,
             截断成「缓存 128(2/5 条…」比换行难读得多。 */
          this.sub == null ? null : h("div", { style: { fontSize: "11px",
            opacity: 0.45, marginTop: "2px", lineHeight: 1.35 } }, this.sub),
        ]),
      ]));
    },
  };
  /* 用户看的是等待时间:10 秒内绿、25 秒内黄，再慢才红。 */
  const elapsedLv = (s) => (s <= 10 ? "success"
    : s <= 25 ? "warning" : "error");
  const frtLv = (s) => (s <= 1.5 ? "success" : s <= 5 ? "warning" : "error");

  /* usage_logs 没有缓存读写列、没有上游请求 ID、没有错误原文:
     能救回来的全在 pricing_snapshot 里,救不回来的一律给 null,由视图渲染「—」。 */
  function normUsage(row, keyNames) {
    const snap = row.pricing_snapshot || {};
    const mode = row.billing_mode || snap.mode || "free";
    const ctx = snap.total_ctx == null ? null : Number(snap.total_ctx);
    const inTok = row.input_tokens || 0;
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
      /* 缓存 token 未落列,只能由判定量反推合计;拿不到判定量就承认不知道。 */
      cache: ctx == null ? null : Math.max(0, ctx - inTok),
      px: hasPx ? { i: snap.input_price, o: snap.output_price,
        cr: snap.cache_read_price, cw: snap.cache_write_price } : null,
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
      tokenSource: row.token_source || null,
    };
  }
  const tpsOf = (r) => (r.ms > 0 && r.o > 0 ? Math.round(r.o / (r.ms / 1000)) : 0);
  const kindOf = (r) => (!r.ok ? ["失败", C.err]
    : r.free ? ["免费", sem("success")] : ["消耗", null]);
  const END_TEXT = { done: "正常结束", eof: "上游中断", client_gone: "客户端断开",
    scanner_error: "响应解析失败" };

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
        const bar = h("div", { style: { flex: "1 1 0", minWidth: 0,
          display: "flex", flexDirection: "column", justifyContent: "flex-end",
          height: h0 + "px", cursor: v > 0 ? "default" : "default" } }, [
          h("div", { style: { height: pct + "%", borderRadius: "3px 3px 0 0",
            background: v > 0 ? this.color : "rgba(127,127,127,.13)",
            minHeight: v > 0 ? "2px" : "2px",
            transition: "height .2s" } }),
        ]);
        return wrapTip(bar, [tipRow(x.label, x.text != null ? x.text : nf(v),
          this.color)], "top");
      });
      /* X 轴只标首末与中点:14 个日期标签在 300px 宽里必然叠字。 */
      const n = items.length;
      const ticks = n ? [0, Math.floor((n - 1) / 2), n - 1]
        .filter((i, k, a) => a.indexOf(i) === k) : [];
      return h("div", null, [
        h("div", { style: { display: "flex", alignItems: "flex-end",
          gap: "3px" } }, bars),
        h("div", { style: { display: "flex", justifyContent: "space-between",
          marginTop: "6px", fontSize: "10.5px", opacity: 0.45 } },
          ticks.map((i) => h("span", { style: MONO }, items[i].label))),
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
            h("div", { style: Object.assign({ fontSize: "17px",
              fontWeight: 700, lineHeight: 1.1 }, MONO) }, this.center),
            this.sub == null ? null : h("div", { style: { fontSize: "10.5px",
              opacity: 0.5, marginTop: "2px" } }, this.sub),
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

  window.BitPortal = {
    BASE: BASE, api: api, qs: qs, download: download,
    token: token, setToken: setToken, clearToken: clearToken,
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
    pill: pill, Pills: Pills, StatCard: StatCard,
    BarChart: BarChart, Donut: Donut, Avatar: Avatar,
    shrinkAvatar: shrinkAvatar, AVATAR_MAX: AVATAR_MAX,
    elapsedLv: elapsedLv, frtLv: frtLv, tpsOf: tpsOf, kindOf: kindOf,
    normUsage: normUsage, END_TEXT: END_TEXT, EXPIRY_PRESETS: EXPIRY_PRESETS,
    CCSWITCH_APPS: CCSWITCH_APPS, ccServerAddress: ccServerAddress,
    buildCCSwitchURL: buildCCSwitchURL, ccLoadModels: ccLoadModels,
  };
})();
