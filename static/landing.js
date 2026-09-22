(function () {
  "use strict";

  var root = document.documentElement;
  var themeButton = document.getElementById("themebtn");
  function paintTheme() {
    var dark = root.classList.contains("dark");
    themeButton.textContent = dark ? "☀" : "◐";
    themeButton.setAttribute("aria-pressed", String(dark));
    themeButton.setAttribute("aria-label", dark ? "切换浅色模式" : "切换深色模式");
    document.querySelector('meta[name="theme-color"]').content = dark ? "#1e221d" : "#f5f4ee";
  }
  themeButton.addEventListener("click", function () {
    var dark = root.classList.toggle("dark");
    try { localStorage.setItem("bitapi_theme", dark ? "dark" : "light"); } catch (e) {}
    paintTheme();
  });
  paintTheme();
  document.getElementById("year").textContent = new Date().getFullYear();

  var ORIGIN = /^https?:$/.test(location.protocol) ? location.origin : "http://127.0.0.1:8080";
  var ENDPOINT = ORIGIN + "/v1";
  document.getElementById("api-endpoint").textContent = ENDPOINT;
  var MODEL = { chat: "YOUR_MODEL_ID", claude: "YOUR_MODEL_ID" };
  // Model names are quoted separately so a catalog entry cannot break sample syntax.
  var SAMPLES = {
    curl: [
      "# 流式对话 · OpenAI 兼容",
      "curl __ORIGIN__/v1/chat/completions \\",
      '  -H "Authorization: Bearer $BITAPI_KEY" \\',
      '  -H "Content-Type: application/json" \\',
      "  -d '__PAYLOAD__'"
    ].join("\n"),
    openai: [
      "# pip install openai",
      "from openai import OpenAI",
      "",
      'client = OpenAI(api_key="sk-…",',
      '                base_url="__ORIGIN__/v1")',
      "",
      "stream = client.chat.completions.create(",
      "    model=__CHAT__,",
      '    messages=[{"role": "user", "content": "你好"}],',
      "    stream=True)",
      "for chunk in stream:",
      "    if chunk.choices:",
      '        print(chunk.choices[0].delta.content or "", end="")'
    ].join("\n"),
    claude: [
      "# pip install anthropic",
      "from anthropic import Anthropic",
      "",
      'client = Anthropic(api_key="sk-…",',
      '                   base_url="__ORIGIN__")',
      "",
      "msg = client.messages.create(",
      "    model=__CLAUDE__,",
      "    max_tokens=1024,",
      '    messages=[{"role": "user", "content": "你好"}])',
      "print(msg.content[0].text)"
    ].join("\n")
  };
  var pre = document.getElementById("code");
  var tabs = Array.from(document.querySelectorAll(".tab"));
  var active = "curl";
  function renderCode(key) {
    var payload = JSON.stringify({ model: MODEL.chat, stream: true,
      messages: [{ role: "user", content: "你好" }] }, null, 2);
    var replacements = { __ORIGIN__: ORIGIN, __CHAT__: JSON.stringify(MODEL.chat),
      __CLAUDE__: JSON.stringify(MODEL.claude), __PAYLOAD__: payload.replace(/'/g, "'\\''") };
    var text = SAMPLES[key].replace(/__ORIGIN__|__CHAT__|__CLAUDE__|__PAYLOAD__/g,
      function (placeholder) { return replacements[placeholder]; });
    pre.textContent = "";
    text.split("\n").forEach(function (line, i) {
      if (i) pre.appendChild(document.createTextNode("\n"));
      if (/^\s*#/.test(line)) {
        var comment = document.createElement("span");
        comment.className = "c";
        comment.textContent = line;
        pre.appendChild(comment);
      } else pre.appendChild(document.createTextNode(line));
    });
  }
  function selectTab(tab) {
    active = tab.dataset.tab;
    tabs.forEach(function (item) {
      item.setAttribute("aria-selected", String(item === tab));
      item.tabIndex = item === tab ? 0 : -1;
    });
    pre.setAttribute("aria-labelledby", tab.id);
    renderCode(active);
  }
  tabs.forEach(function (tab, index) {
    tab.addEventListener("click", function () { selectTab(tab); });
    tab.addEventListener("keydown", function (event) {
      var next;
      if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
      else if (event.key === "ArrowLeft") next = (index + tabs.length - 1) % tabs.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = tabs.length - 1;
      else return;
      event.preventDefault();
      selectTab(tabs[next]);
      tabs[next].focus();
    });
  });
  renderCode(active);

  var status = document.getElementById("copy-status");
  var toastTimer;
  function notify(message) {
    clearTimeout(toastTimer);
    status.textContent = message;
    status.classList.add("visible");
    toastTimer = setTimeout(function () { status.classList.remove("visible"); }, 2600);
  }
  async function copy(text, success) {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        var previousFocus = document.activeElement;
        var input = document.createElement("textarea");
        input.value = text;
        input.style.position = "fixed";
        input.style.opacity = "0";
        document.body.appendChild(input);
        try {
          input.select();
          if (!document.execCommand("copy")) throw new Error("Copy unavailable");
        } finally {
          input.remove();
          if (previousFocus) previousFocus.focus();
        }
      }
      notify(success);
    } catch (e) { notify("复制失败，请选中内容手动复制"); }
  }
  document.getElementById("copybtn").addEventListener("click", function () {
    copy(pre.textContent, "代码已复制，填入你的密钥即可开始");
  });
  document.getElementById("endpoint-copy").addEventListener("click", function () {
    copy(ENDPOINT, "API 端点已复制");
  });

  // Keep provider identification aligned with static/portal-shared.js.
  var PROV = [
    [/gpt|chatgpt|^o[134]-/, "OpenAI", "openai", 1],
    [/claude|anthropic|fable|opus|sonnet/, "Anthropic", "claude-color", 0],
    [/gemma/, "Gemma", "gemma-color", 0],
    [/gemini|learnlm/, "Google", "gemini-color", 0],
    [/veo-|nano-banana/, "Google", "google-color", 0],
    [/grok|\bxai\b/, "xAI", "grok", 1],
    [/minimax|abab/, "MiniMax", "minimax-color", 0],
    [/deepseek/, "DeepSeek", "deepseek-color", 0],
    [/qwen|qwq/, "Qwen", "qwen-color", 0],
    [/glm|chatglm/, "Zhipu", "zhipu-color", 0],
    [/kimi|moonshot/, "Moonshot", "moonshot", 1],
    [/step-|stepfun/, "StepFun", "stepfun-color", 0],
    [/mercury|inception/, "Inception", "inception", 1],
    [/kling/, "Kling", "kling-color", 0],
    [/seedream|seedance/, "ByteDance", "bytedance-color", 0],
    [/\bwan-/, "Alibaba", "alibaba-color", 0],
    [/pixverse/, "PixVerse", "pixverse-color", 0],
    [/nvidia|nvcf/, "NVIDIA", "nvidia-color", 0],
    [/poolside|laguna/, "Poolside", "poolside", 1]
  ];
  function providerOf(model) {
    var match = PROV.find(function (p) { return p[0].test(String(model || "").toLowerCase()); });
    return match ? { name: match[1], icon: match[2], mono: !!match[3] } : null;
  }
  function money(value) {
    var s = value >= 1 ? value.toFixed(2) : value.toFixed(4);
    return "$" + s.replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
  }
  function pick(rows, pattern) {
    var row = rows.find(function (item) { return item.chat && pattern.test(String(item.model).toLowerCase()); });
    return row ? row.model : null;
  }
  function set(id, value) { document.getElementById(id).textContent = value; }
  function renderModels(data) {
    if (!Array.isArray(data.data)) throw new Error("Invalid model catalog");
    var rows = data.data;
    var multiplier = Number.isFinite(data.rate_multiplier) ? data.rate_multiplier : 1;
    var channels = new Set();
    var providers = new Map();
    var lowest = null;
    rows.forEach(function (row) {
      (Array.isArray(row.channels) && row.channels.length ? row.channels : [row.channel])
        .forEach(function (channel) { if (channel) channels.add(channel); });
      if (row.billing_mode === "token" && Number.isFinite(row.input_price) && row.input_price >= 0)
        lowest = lowest === null ? row.input_price : Math.min(lowest, row.input_price);
      var provider = providerOf(row.model);
      if (provider) providers.set(provider.name, provider);
    });
    set("n-models", rows.length);
    set("n-ch", channels.size);
    set("n-price", lowest === null ? "—" : money(data.policy === "free" ? 0 : lowest * multiplier));
    var note = ["实时目录 · 单价单位：美元 / 1M token。"];
    note.push(data.group ? "按「" + data.group + "」分组口径。" : "按默认分组口径。");
    if (multiplier !== 1) note.push("已含 ×" + multiplier + " 倍率。");
    if (data.policy === "free") note.push("该分组当前免费，不扣余额。");
    else if (data.policy === "quota") note.push("该分组当前为配额限额。");
    if (!rows.length) note.push("当前暂无可用模型，可稍后前往模型广场查看。");
    set("statnote", note.join(""));
    MODEL.chat = pick(rows, /gpt|claude|fable|sonnet|deepseek|qwen|glm|kimi/) || pick(rows, /./) || "YOUR_MODEL_ID";
    MODEL.claude = pick(rows, /claude|fable|opus|sonnet/) || MODEL.chat;
    document.querySelector(".code-note").lastChild.textContent = MODEL.chat === "YOUR_MODEL_ID"
      ? "请从模型广场选择可用对话模型，替换 YOUR_MODEL_ID" : "示例模型取自当前模型目录";
    renderCode(active);
    var box = document.getElementById("vendrow");
    box.textContent = "";
    Array.from(providers.values()).slice(0, 12).forEach(function (provider) {
      var cell = document.createElement("div");
      cell.className = "provider";
      var icon = document.createElement("img");
      icon.src = "/static/model-icons/" + provider.icon + ".svg";
      icon.alt = "";
      icon.width = 24;
      icon.height = 24;
      icon.loading = "lazy";
      if (provider.mono) icon.className = "mono";
      var label = document.createElement("span");
      label.textContent = provider.name;
      cell.append(icon, label);
      box.appendChild(cell);
    });
  }
  fetch("/api/models", { headers: { accept: "application/json" } })
    .then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    })
    .then(renderModels)
    .catch(function () {
      set("statnote", "模型目录暂时无法加载，请稍后重试或前往模型广场查看。");
      document.querySelector(".code-note").lastChild.textContent = "请从模型广场选择可用对话模型，替换 YOUR_MODEL_ID";
    });
})();
