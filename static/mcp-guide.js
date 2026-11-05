(function () {
  "use strict";

  const root = document.documentElement;
  const themeButton = document.getElementById("theme-toggle");
  const systemDark = window.matchMedia("(prefers-color-scheme: dark)");
  let savedTheme = null;
  try { savedTheme = localStorage.getItem("bitapi_theme"); } catch (_) { /* 私密模式仍可阅读。 */ }
  function applyTheme(dark) {
    root.classList.toggle("dark", dark);
    themeButton.textContent = dark ? "浅色模式" : "深色模式";
    themeButton.setAttribute("aria-label", dark ? "切换浅色模式" : "切换深色模式");
  }
  applyTheme(savedTheme ? savedTheme === "dark" : systemDark.matches);
  themeButton.hidden = false;
  themeButton.addEventListener("click", () => {
    const dark = !root.classList.contains("dark");
    savedTheme = dark ? "dark" : "light";
    applyTheme(dark);
    try { localStorage.setItem("bitapi_theme", savedTheme); } catch (_) { /* 保留当前页选择。 */ }
  });
  systemDark.addEventListener("change", (event) => {
    if (!savedTheme) applyTheme(event.matches);
  });

  // 匿名用户登录后回到密钥页；已登录用户直接进入密钥页。
  let hasToken = false;
  hasToken = document.cookie.split("; ").some((value) => value.startsWith("bitapi_csrf="));
  document.querySelectorAll("[data-key-link]").forEach((link) => {
    link.href = hasToken ? "/portal#/keys" : "/portal#/login?next=keys";
  });

  const status = document.getElementById("copy-status");
  let statusTimer;
  document.querySelectorAll("[data-copy]").forEach((button) => {
    button.hidden = false;
    const label = button.textContent;
    let buttonTimer;
    button.addEventListener("click", async () => {
      const content = document.getElementById(button.dataset.copy).textContent.trim();
      clearTimeout(statusTimer);
      clearTimeout(buttonTimer);
      try {
        await navigator.clipboard.writeText(content);
        button.textContent = "已复制";
        button.classList.add("is-copied");
        status.textContent = button.getAttribute("aria-label") + "：已复制";
      } catch (_) {
        button.textContent = label;
        button.classList.remove("is-copied");
        status.textContent = "浏览器未允许复制，请选中对应内容手动复制。";
      }
      buttonTimer = setTimeout(() => {
        button.textContent = label;
        button.classList.remove("is-copied");
      }, 2000);
      statusTimer = setTimeout(() => { status.textContent = ""; }, 3500);
    });
  });
})();
