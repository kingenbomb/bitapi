/* bit-api 控制台入口:主题、hash 路由 + JWT 守卫、登录/注册、外壳、挂载。
   页面组件在 static/portal-pages.js(用户侧)与 static/portal-admin.js(管理台)。 */
(function () {
  "use strict";
  const { createApp, ref, computed, watch, watchEffect, onMounted, h } = Vue;
  const P = window.BitPortal;
  const { api, msg, token, setToken, clearToken, dark, theme, themeOverrides, ic,
    tag, POLICY, ANN_LEVEL, relTime, nf, fmt, narrow } = P;
  const PG = window.BitPortalPages;
  const ADM = window.BitPortalAdmin;

  /* 一条 nav 记录同时供菜单、标题、副标题使用,避免三处各写一份还写歪。 */
  const NAV = [
    ["dashboard", "概览", "grid", "余额、请求量与今日消费一眼看全"],
    ["plaza", "模型广场", "cube", "能调的模型、供应商与生效单价"],
    ["playground", "在线体验", "bolt", "挑个模型直接对话,按真实单价扣费"],
    ["keys", "API 密钥", "key", "创建、命名、禁用密钥,或一键导入 CC Switch"],
    ["usage", "使用记录", "doc", "每次请求的 token 明细与命中的定价档位"],
    ["wallet", "钱包", "wallet", "充值、兑换码与余额流水"],
    ["billing", "计费与套餐", "dollar", "买时长卡、看当前额度与生效单价"],
    ["invite", "邀请返佣", "gift", "返佣码、返佣入账与绑定规则"],
    ["profile", "个人资料", "user", "昵称头像、登录绑定与密码"],
    ["admin", "管理控制台", "gear", "站点设置、分组、定价、用户与兑换码"],
  ];
  const COMP = { dashboard: PG.Dashboard, plaza: PG.Plaza,
    playground: PG.Playground, keys: PG.Keys,
    usage: PG.Usage, wallet: PG.Wallet, billing: PG.Billing,
    invite: PG.Invite, profile: PG.Profile, admin: ADM.Admin };
  const META = {};
  NAV.forEach((n) => { META[n[0]] = { title: n[1], sub: n[3] }; });
  /* 登录页那几张:未登录才给看,已登录访问会被弹回概览。 */
  const AUTHONLY = { login: 1, register: 1, forgot: 1 };
  /* 社区回调既可能是游客登录,也可能是已登录用户绑定,两种状态都要放行。
     重置密码链接也一样:邮件里点开时可能正带着另一台设备的登录态。 */
  const AUTHFLOW = { "community-callback": 1, reset: 1 };
  /* 登录与否都给看的页面。模型广场是站点对外的模型清单,拦在登录后面就没人看得到。 */
  const OPEN = { plaza: 1 };

  function parseHash(raw) {
    const s = String(raw || "").replace(/^#\/?/, "");
    const i = s.indexOf("?");
    return { path: (i < 0 ? s : s.slice(0, i)) || "",
      query: new URLSearchParams(i < 0 ? "" : s.slice(i + 1)) };
  }

  /* 登录与注册共用一张居中卡:两页字段差一个邀请码,不值得拆成两套壳。 */
  const Auth = {
    props: ["mode", "ref0"],
    setup(props) {
      const f = ref({ email: "", password: "", invite: props.ref0 || "", code: "" });
      const busy = ref(false);
      const communityBusy = ref(false);
      const err = ref("");
      const reg = computed(() => props.mode === "register");
      /* 开了二次验证的账号:密码对了只拿到一张 5 分钟的票,再要一个验证码。 */
      const ticket = ref("");

      const finish = (r) => {
        setToken(r.token);
        if (reg.value) msg.success("注册成功,你的邀请码 " + r.aff_code);
        location.hash = "#/dashboard";
      };
      const submit = () => {
        err.value = "";
        if (ticket.value) return submitCode();
        const email = (f.value.email || "").trim();
        if (!email) return (err.value = "请填写邮箱");
        if (!f.value.password) return (err.value = "请填写密码");
        if (reg.value && f.value.password.length < 6)
          return (err.value = "密码至少 6 位");
        busy.value = true;
        const body = { email: email, password: f.value.password };
        if (reg.value) body.invite_code = (f.value.invite || "").trim();
        api("POST", reg.value ? "/register" : "/login", body)
          .then((r) => {
            if (r.needs_totp) { ticket.value = r.ticket; f.value.code = ""; return; }
            finish(r);
          })
          .catch((e) => { err.value = e.message; })
          .then(() => { busy.value = false; });
      };
      const submitCode = () => {
        const code = (f.value.code || "").trim();
        if (code.length < 6) return (err.value = "请输入 6 位验证码");
        busy.value = true;
        api("POST", "/login/totp", { ticket: ticket.value, code: code })
          .then(finish)
          .catch((e) => {
            err.value = e.message;
            /* 票过期(5 分钟)要从密码重来 */
            if (/ticket/i.test(e.message)) ticket.value = "";
          })
          .then(() => { busy.value = false; });
      };
      const backToPassword = () => { ticket.value = ""; err.value = ""; };

      const communityLogin = () => {
        err.value = "";
        communityBusy.value = true;
        api("POST", "/community/auth/start", { purpose: "login" })
          .then((r) => {
            if (!r.authorize_url) throw new Error("社区登录地址未配置");
            sessionStorage.setItem("bitapi_community_purpose", "login");
            location.assign(r.authorize_url);
          })
          .catch((e) => { err.value = e.message; })
          .then(() => { communityBusy.value = false; });
      };

      return { f, busy, communityBusy, err, reg, submit, communityLogin, ic,
        ticket, backToPassword };
    },
    template: `
<div class="authwrap">
  <n-card class="authcard" :bordered="false" size="large">
    <div class="authbrand">
      <img class="brandmark" src="/static/bit-api-icon-180.png" alt="">
      <div>
        <div class="authname">bit-api</div>
        <div class="authsub">{{ ticket ? '二次验证' : reg ? '注册新账号' : '登录控制台' }}</div>
      </div>
    </div>
    <n-form v-if="ticket" :show-feedback="false" @submit.prevent="submit">
      <n-space vertical :size="14">
        <n-text depth="3" style="font-size:12.5px">
          {{ f.email }} 开启了二次验证,输入 authenticator 里当前的 6 位验证码。</n-text>
        <n-form-item label="验证码" label-placement="top">
          <n-input v-model:value="f.code" placeholder="123456" maxlength="8"
            :input-props="{inputmode:'numeric',autocomplete:'one-time-code'}"
            @keyup.enter="submit"/>
        </n-form-item>
        <n-alert v-if="err" type="error" :bordered="false" :show-icon="false"
          style="font-size:12.5px">{{ err }}</n-alert>
        <n-button type="primary" block :loading="busy" @click="submit">验证并登录</n-button>
        <div class="authswitch">
          <n-button text type="primary" @click="backToPassword">返回重新输入密码</n-button>
        </div>
      </n-space>
    </n-form>
    <n-form v-else :show-feedback="false" @submit.prevent="submit">
      <n-space vertical :size="14">
        <n-form-item label="邮箱" label-placement="top">
          <n-input v-model:value="f.email" placeholder="you@example.com"
            :input-props="{type:'email',autocomplete:'username'}"/>
        </n-form-item>
        <n-form-item :label="reg ? '密码(至少 6 位)' : '密码'"
          label-placement="top">
          <n-input v-model:value="f.password" type="password"
            show-password-on="click" placeholder="••••••"
            :input-props="{autocomplete: reg ? 'new-password' : 'current-password'}"
            @keyup.enter="submit"/>
        </n-form-item>
        <n-form-item v-if="reg" label="邀请码 / 返佣码" label-placement="top">
          <n-input v-model:value="f.invite"
            placeholder="邀请制开启时仅接受一次性注册邀请码"
            @keyup.enter="submit"/>
        </n-form-item>
        <n-alert v-if="err" type="error" :bordered="false" :show-icon="false"
          style="font-size:12.5px">{{ err }}</n-alert>
        <n-button type="primary" block :loading="busy" @click="submit">
          {{ reg ? '注册' : '登录' }}</n-button>
        <template v-if="!reg">
          <div class="authswitch" style="margin-top:-6px;text-align:right">
            <n-button text size="tiny" tag="a" href="#/forgot">忘记密码?</n-button>
          </div>
          <n-divider style="margin:0;font-size:12px">或</n-divider>
          <n-button block secondary :loading="communityBusy"
            @click="communityLogin">社区账号登录</n-button>
        </template>
        <div class="authswitch">
          <span v-if="reg">已有账号?
            <n-button text type="primary" tag="a" href="#/login">去登录
            </n-button></span>
          <span v-else>还没有账号?
            <n-button text type="primary" tag="a" href="#/register">注册
            </n-button></span>
        </div>
        <!-- 模型广场不需要登录,登录页得给个入口,否则这页只有知道 hash 的人找得到。 -->
        <div class="authswitch" style="margin-top:-6px">
          不想注册也能先看看
          <n-button text type="primary" tag="a" href="#/plaza">模型广场
          </n-button>
        </div>
      </n-space>
    </n-form>
  </n-card>
</div>`,
  };

  /* 找回密码两步共用一张卡:forgot 填邮箱发链接,reset 带着邮件里的 token 设新密码。
     邮箱不存在服务端也回「已发送」(不可枚举),所以这里的成功提示只能说
     「如果该邮箱已注册,会收到邮件」。 */
  const Recover = {
    props: ["mode", "token"],
    setup(props) {
      const email = ref("");
      const pw = ref("");
      const pw2 = ref("");
      const busy = ref(false);
      const err = ref("");
      const done = ref(false);
      const isReset = computed(() => props.mode === "reset");

      const sendLink = () => {
        err.value = "";
        const e = email.value.trim();
        if (!e) return (err.value = "请填写注册时用的邮箱");
        busy.value = true;
        api("POST", "/password/forgot", { email: e })
          .then(() => { done.value = true; })
          .catch((x) => { err.value = x.message; })
          .then(() => { busy.value = false; });
      };
      const doReset = () => {
        err.value = "";
        if (!props.token) return (err.value = "链接里没有令牌,请从邮件重新点开");
        if (pw.value.length < 6) return (err.value = "新密码至少 6 位");
        if (pw.value !== pw2.value) return (err.value = "两次输入的密码不一致");
        busy.value = true;
        api("POST", "/password/reset", { token: props.token, new_password: pw.value })
          .then((r) => {
            /* 旧会话此刻已作废,本机若还挂着 token 也一起清掉再去登录 */
            clearToken();
            msg.success("密码已重置,请用新密码登录");
            location.hash = "#/login";
            email.value = r.email || "";
          })
          .catch((x) => { err.value = x.message; })
          .then(() => { busy.value = false; });
      };
      return { email, pw, pw2, busy, err, done, isReset, sendLink, doReset };
    },
    template: `
<div class="authwrap">
  <n-card class="authcard" :bordered="false" size="large">
    <div class="authbrand">
      <img class="brandmark" src="/static/bit-api-icon-180.png" alt="">
      <div>
        <div class="authname">bit-api</div>
        <div class="authsub">{{ isReset ? '设置新密码' : '找回密码' }}</div>
      </div>
    </div>
    <n-form :show-feedback="false" @submit.prevent="isReset ? doReset() : sendLink()">
      <n-space vertical :size="14">
        <template v-if="isReset">
          <n-form-item label="新密码(至少 6 位)" label-placement="top">
            <n-input v-model:value="pw" type="password" show-password-on="click"
              placeholder="••••••" :input-props="{autocomplete:'new-password'}"/>
          </n-form-item>
          <n-form-item label="再输一次" label-placement="top">
            <n-input v-model:value="pw2" type="password" show-password-on="click"
              placeholder="••••••" :input-props="{autocomplete:'new-password'}"
              @keyup.enter="doReset"/>
          </n-form-item>
          <n-alert v-if="err" type="error" :bordered="false" :show-icon="false"
            style="font-size:12.5px">{{ err }}</n-alert>
          <n-button type="primary" block :loading="busy" @click="doReset">
            重置密码</n-button>
        </template>
        <template v-else-if="done">
          <n-alert type="success" :bordered="false" :show-icon="false">
            如果 <b>{{ email }}</b> 是已注册的邮箱,重置链接已经发出,30 分钟内有效。
            没收到请查看垃圾邮件,或稍后再试一次。
          </n-alert>
        </template>
        <template v-else>
          <n-form-item label="注册邮箱" label-placement="top">
            <n-input v-model:value="email" placeholder="you@example.com"
              :input-props="{type:'email',autocomplete:'username'}"
              @keyup.enter="sendLink"/>
          </n-form-item>
          <n-alert v-if="err" type="error" :bordered="false" :show-icon="false"
            style="font-size:12.5px">{{ err }}</n-alert>
          <n-button type="primary" block :loading="busy" @click="sendLink">
            发送重置链接</n-button>
        </template>
        <div class="authswitch">
          <n-button text type="primary" tag="a" href="#/login">返回登录</n-button>
        </div>
      </n-space>
    </n-form>
  </n-card>
</div>`,
  };

  /* 授权码由后端回调收进 HttpOnly cookie,本页只做同源 POST 换登录态。
     新社区用户第一次会停在邀请码输入框,不会把 token 或授权码塞进 hash。 */
  const CommunityCallback = {
    setup() {
      const busy = ref(true);
      const needsInvite = ref(false);
      const invite = ref("");
      const community = ref({});
      const err = ref("");
      const purpose = sessionStorage.getItem("bitapi_community_purpose") || "login";

      const finish = (withInvite) => {
        const code = invite.value.trim();
        if (withInvite && !code) return (err.value = "请输入邀请码");
        busy.value = true;
        err.value = "";
        const body = withInvite ? { invite_code: code } : {};
        api("POST", "/community/auth/finish", body)
          .then((r) => {
            if (r.needs_invite) {
              community.value = r.community || {};
              needsInvite.value = true;
              return;
            }
            if (r.token) setToken(r.token);
            if (!r.token && purpose !== "bind")
              throw new Error("社区登录未返回登录凭证");
            sessionStorage.removeItem("bitapi_community_purpose");
            if (r.bound || purpose === "bind") {
              msg.success("社区账号已绑定");
              window.dispatchEvent(new Event("bitapi:me-changed"));
              location.hash = "#/profile";
              return;
            }
            msg.success("社区账号登录成功");
            location.hash = "#/dashboard";
          })
          .catch((e) => { err.value = e.message; })
          .then(() => { busy.value = false; });
      };

      onMounted(() => finish(false));
      return { busy, needsInvite, invite, community, err, purpose, finish };
    },
    template: `
<div class="authwrap">
  <n-card class="authcard" :bordered="false" size="large">
    <div class="authbrand">
      <img class="brandmark" src="/static/bit-api-icon-180.png" alt="">
      <div>
        <div class="authname">bit-api</div>
        <div class="authsub">{{ purpose === 'bind' ? '绑定社区账号' : '社区账号登录' }}</div>
      </div>
    </div>
    <div v-if="busy && !needsInvite" style="padding:30px 0;text-align:center">
      <n-spin size="small"/>
      <n-text depth="3" style="display:block;margin-top:12px;font-size:12.5px">
        正在确认社区身份…</n-text>
    </div>
    <n-space v-else-if="needsInvite" vertical :size="14">
      <n-alert type="success" :bordered="false" :show-icon="false">
        已验证社区账号
        <b>{{ community.name || community.username || '社区用户' }}</b>
        <span v-if="community.username"> (@{{ community.username }})</span>
      </n-alert>
      <n-text depth="3" style="font-size:12.5px">
        首次使用社区账号创建 bit-api 账号需要邀请码。
      </n-text>
      <n-form-item label="邀请码" :show-feedback="false">
        <n-input v-model:value="invite" placeholder="请输入邀请码"
          @keyup.enter="finish(true)"/>
      </n-form-item>
      <n-alert v-if="err" type="error" :bordered="false" :show-icon="false"
        style="font-size:12.5px">{{ err }}</n-alert>
      <n-button type="primary" block :loading="busy" @click="finish(true)">
        完成注册并登录</n-button>
    </n-space>
    <n-space v-else vertical :size="14">
      <n-alert type="error" :bordered="false" :show-icon="false">
        {{ err || '社区授权未完成,请重新发起。' }}</n-alert>
      <n-button block secondary tag="a"
        :href="purpose === 'bind' ? '#/profile' : '#/login'">
        {{ purpose === 'bind' ? '返回个人资料' : '返回登录' }}
      </n-button>
    </n-space>
  </n-card>
</div>`,
  };

  /* 顶栏公告铃铛。已读状态在后端(announcement_reads 表),不放 localStorage ——
     换设备或清缓存不该让人重看一遍公告,而且「谁读过哪条」站长也要看。
     点条目标这一条,点头部按钮标全部;未读高亮用打开那一刻的快照,
     否则条目会在眼前褪色。 */
  const Bell = {
    props: ["items", "unread"],
    emits: ["refresh", "read"],
    components: { Ic: P.Ic },
    setup(props, ctx) {
      const show = ref(false);
      const fresh = ref({});
      watch(show, (v) => {
        if (!v) return;
        ctx.emit("refresh");
        const m = {};
        (props.items || []).forEach((a) => { if (a.unread) m[a.id] = 1; });
        fresh.value = m;
      });
      const list = computed(() => props.items || []);
      const hit = (a) => { if (a.unread) ctx.emit("read", [a.id]); };
      return { show, fresh, list, hit, ANN_LEVEL, relTime,
        readAll: () => ctx.emit("read", null) };
    },
    template: `
<n-popover trigger="click" placement="bottom-end" :show="show"
  content-class="flushpop" :show-arrow="false"
  @update:show="(v) => show = v">
  <template #trigger>
    <n-badge :value="unread" :max="99" :offset="[-3, 3]">
      <n-button quaternary circle size="small" aria-label="公告">
        <Ic name="bell" :size="17"/>
      </n-button>
    </n-badge>
  </template>
  <div class="annpanel">
    <div class="annhead">
      <span>公告</span>
      <n-space :size="8" align="center">
        <n-text depth="3" style="font-size:11.5px">
          {{ unread ? unread + ' 条未读' : (list.length ? list.length + ' 条' : '') }}
        </n-text>
        <n-button v-if="unread" text type="primary" style="font-size:11.5px"
          @click="readAll">全部已读</n-button>
      </n-space>
    </div>
    <n-scrollbar style="max-height:330px">
      <div v-if="!list.length" class="annempty">
        <n-text depth="3" style="font-size:12.5px">暂无公告</n-text>
      </div>
      <div v-for="a in list" :key="a.id" class="annitem"
        :class="{ annnew: fresh[a.id] }" @click="hit(a)">
        <div class="annrow">
          <n-tag size="small" round :bordered="false" :type="a.level">
            {{ ANN_LEVEL[a.level] || a.level }}</n-tag>
          <span class="anntitle">{{ a.title }}</span>
          <n-tag v-if="a.pinned" size="small" round :bordered="false">置顶</n-tag>
          <span v-if="a.unread" class="anndot"></span>
        </div>
        <div class="annbody">{{ a.body }}</div>
        <n-text depth="3" style="font-size:11px">
          {{ relTime(a.updated_at || a.created_at) }}</n-text>
      </div>
    </n-scrollbar>
  </div>
</n-popover>`,
  };

  /* 顶栏余额胶囊。三种计费策略都显示 —— users.balance 与分组策略无关(免费组也能
     靠兑换码攒余额,之后换到余额组就能花),把它藏起来反而让人以为钱没了。
     参考图第二行的「冻结金额」本项目没有:设计上不做额度预占(见 README),
     没有被占住的钱,所以不摆一个恒为 0 的假字段。 */
  const Balance = {
    props: ["bill"],
    emits: ["nav"],
    components: { Ic: P.Ic },
    setup(props, ctx) {
      const b = computed(() => props.bill || {});
      return { b, POLICY, fmt, go: (p) => ctx.emit("nav", p) };
    },
    template: `
<n-popover trigger="hover" placement="bottom-end" content-class="flushpop"
  :show-arrow="false">
  <template #trigger>
    <n-button size="small" secondary round @click="go('wallet')">
      <template #icon><Ic name="wallet" :size="15"/></template>
      <span :style="{fontFamily:'var(--mono)',fontWeight:650}">
        \${{ fmt(b.balance) }}</span>
    </n-button>
  </template>
  <div class="crpanel">
    <div class="crrow">
      <span class="crk">可用余额</span>
      <span class="crv">\${{ fmt(b.balance) }}</span>
    </div>
    <div class="crrow">
      <span class="crk">累计消费</span>
      <span class="crv crdim">\${{ fmt(b.total_spent) }}</span>
    </div>
    <div class="crrow crtop">
      <span class="crk">计费方式</span>
      <span class="crv">{{ POLICY[b.policy] || b.policy }}</span>
    </div>
    <div class="crrow">
      <span class="crk">分组 · 倍率</span>
      <span class="crv">{{ b.group || '—' }} ×{{ b.rate_multiplier || 1 }}</span>
    </div>
    <n-button size="tiny" type="primary" block style="margin-top:11px"
      @click="go('wallet')">去充值</n-button>
  </div>
</n-popover>`,
  };

  /* 额度胶囊。只在额度限额策略且真配了上限时出现 —— 没有额度的账户挂一个空进度条
     没有意义。胶囊上是三个圆点(按日/周/月各自的用量染色)+ 最紧的那档百分比,
     悬浮出三条进度条。 */
  const QuotaMini = {
    props: ["bill"],
    emits: ["nav"],
    components: { Ic: P.Ic },
    setup(props, ctx) {
      const b = computed(() => props.bill || {});
      const unitText = computed(() =>
        (b.value.limit_unit === "tokens" ? "Token" : "请求"));
      const rows = computed(() => {
        const u = b.value.usage || {};
        return [["日", u.daily], ["周", u.weekly], ["月", u.monthly]]
          .filter((x) => x[1] && (x[1].limit || 0) > 0)
          .map((x) => {
            const lim = x[1].limit;
            const used = x[1].used || 0;
            return { label: x[0], used: used, limit: lim,
              pct: Math.min(100, Math.round((used / lim) * 100)) };
          });
      });
      const show = computed(() => b.value.policy === "quota" && rows.value.length);
      /* 胶囊上只报最紧的那一档:三个数字挤在顶栏读不出来,而人真正关心的是
         「哪一档快满了」。 */
      const worst = computed(() => rows.value.reduce(
        (a, x) => (a && a.pct >= x.pct ? a : x), null));
      const lv = (p) => (p >= 90 ? "error" : p >= 70 ? "warning" : "success");
      const dot = (p) => ({ error: "#b91c1c", warning: "#a17a10",
        success: "#15803d" }[lv(p)]);
      return { b, rows, show, worst, unitText, lv, dot, nf,
        go: (p) => ctx.emit("nav", p) };
    },
    template: `
<n-popover v-if="show" trigger="hover" placement="bottom-end"
  content-class="flushpop" :show-arrow="false">
  <template #trigger>
    <n-button size="small" secondary round @click="go('billing')">
      <template #icon><Ic name="gauge" :size="15"/></template>
      <span style="display:inline-flex;align-items:center;gap:5px">
        <span style="display:inline-flex;gap:3px">
          <span v-for="r in rows" :key="r.label" class="qdot"
            :style="{ background: dot(r.pct) }"></span>
        </span>
        <span :style="{fontFamily:'var(--mono)',fontWeight:650}">
          {{ worst ? worst.pct + '%' : '' }}</span>
      </span>
    </n-button>
  </template>
  <div class="crpanel crpanel-wide">
    <div class="crtitle">额度用量 · 按{{ unitText }}</div>
    <div v-for="r in rows" :key="r.label" style="margin-bottom:9px">
      <div class="crrow" style="margin-bottom:4px">
        <span class="crk">{{ r.label }}</span>
        <span class="crv">{{ nf(r.used) }} / {{ nf(r.limit) }}</span>
      </div>
      <n-progress type="line" :percentage="r.pct" :height="5"
        :show-indicator="false" :border-radius="99" :status="lv(r.pct)"/>
    </div>
    <div class="crrow crtop">
      <span class="crk">分组 · RPM</span>
      <span class="crv">{{ b.group || '—' }}
        {{ b.rpm_limit ? '· ' + nf(b.rpm_limit) : '' }}</span>
    </div>
    <n-button size="tiny" block style="margin-top:11px"
      @click="go('billing')">查看套餐</n-button>
  </div>
</n-popover>`,
  };

  /* 登录后弹一次的公告(notify_mode=popup)。弹过就写已读,不再弹。
     一次只弹一条:队列里第一条关掉后再弹下一条,叠弹窗看不清也点不动。 */
  const AnnPopup = {
    props: ["item"],
    emits: ["close"],
    setup(props, ctx) {
      return { ANN_LEVEL, relTime, close: () => ctx.emit("close") };
    },
    template: `
<n-modal :show="!!item" preset="card" style="max-width:520px" :bordered="false"
  :title="item ? item.title : ''" @update:show="(v) => { if (!v) close(); }">
  <n-space vertical :size="12">
    <n-space :size="6" align="center">
      <n-tag size="small" round :bordered="false" :type="item.level">
        {{ ANN_LEVEL[item.level] || item.level }}</n-tag>
      <n-text depth="3" style="font-size:11.5px">
        {{ relTime(item.updated_at || item.created_at) }}</n-text>
    </n-space>
    <div class="annbody" style="font-size:13px;opacity:.85">{{ item.body }}</div>
  </n-space>
  <template #footer><n-space justify="end">
    <n-button size="small" type="primary" @click="close">知道了</n-button>
  </n-space></template>
</n-modal>`,
  };

  const App = {
    components: { Auth, Recover, CommunityCallback, Ic: P.Ic, Avatar: P.Avatar,
      Bell: Bell, Balance: Balance, QuotaMini: QuotaMini, AnnPopup: AnnPopup },

    setup() {
      const route = ref(parseHash(location.hash));
      const me = ref(null);
      const bill = ref(null);
      const anns = ref([]);
      const annUnread = ref(0);
      const popQueue = ref([]);
      const popup = ref(null);
      const shown = new Set();   // 本会话已弹过的公告 id,不入队第二次
      const collapsed = ref(localStorage.getItem("bitapi_sider") === "1");
      /* 窄屏(手机)把侧栏改成盖在内容上的抽屉。has-sider 的侧栏是占位的:234px
         压在 390px 的屏上只剩 150 多给正文,页面会被挤成每行两三个字,选择器还会
         溢出到屏幕外 —— 整个控制台原先一条 media query 都没有。
         narrow 来自 portal-shared.js(与 portal.html 那条 media query 同一个
         断点);抽屉开合用独立的 ref,不写进 bitapi_sider:手机上开一次不该改掉
         桌面端记住的偏好。 */
      const drawer = ref(false);
      watch(narrow, (v) => { if (v) drawer.value = false; });  // 转窄屏先收起
      /* 侧栏的收起态在两种形态下读不同的源:窄屏看抽屉,宽屏看记住的偏好。 */
      const siderCollapsed = computed(
        () => (narrow.value ? !drawer.value : collapsed.value));
      const setSider = (shut) => {
        if (narrow.value) drawer.value = !shut;
        else collapsed.value = shut;
      };
      const authed = ref(!!token());

      /* 守卫写成纯函数:hash 变化与首屏都走它,不留两条判定路径。
         重定向时同步把 route 设成目标,避免首帧先画错页再跳。
         authed 也在这里刷:token() 不是响应式的,而登录、退出、被 401 踢
         最后都会改 hash,统一在守卫里取一次就够,不必各处自己同步。 */
      const guard = () => {
        const r = parseHash(location.hash);
        const has = !!token();
        authed.value = has;
        const home = has ? "dashboard" : "login";
        /* register?ref=CODE 属于 AUTHONLY,未登录也要放行,邀请码不能被守卫吞掉。 */
        const bad = !r.path ||
          (!AUTHONLY[r.path] && !AUTHFLOW[r.path] && !COMP[r.path]) ||
          (!has && !AUTHONLY[r.path] && !AUTHFLOW[r.path] && !OPEN[r.path]) ||
          (has && AUTHONLY[r.path]);
        if (bad) {
          route.value = { path: home, query: new URLSearchParams() };
          if (location.hash !== "#/" + home) location.hash = "#/" + home;
          return;
        }
        route.value = r;
      };
      window.addEventListener("hashchange", guard);

      const path = computed(() => route.value.path);
      const isAuth = computed(() => !!AUTHONLY[path.value] || !!AUTHFLOW[path.value]);
      const page = computed(() => COMP[path.value] || null);
      const meta = computed(() => META[path.value] || { title: "", sub: "" });
      /* 菜单已对普通用户隐藏管理台,但手打 #/admin 也得挡住,
         否则页面会连打一串 403 只显示「加载失败」。角色未知时先不判。 */
      const denied = computed(() => path.value === "admin" && !!me.value &&
        me.value.role !== "admin");

      /* 顶栏那几个 tag 要 /me + /billing,登录后拉一次;401 由 api() 兜。 */
      const loadMe = () => {
        if (!token()) { me.value = null; bill.value = null; return; }
        api("GET", "/me").then((r) => { me.value = r; }).catch(() => {});
        api("GET", "/billing").then((r) => { bill.value = r; }).catch(() => {});
      };
      /* 公告与 me 分开拉:铃铛面板每次打开都重取一次,而 /me 不必跟着重取。
         unread 由后端算(它才知道 read_at),前端不再自己判。 */
      const loadAnn = () => {
        if (!token()) { anns.value = []; annUnread.value = 0; return; }
        api("GET", "/announcements").then((r) => {
          anns.value = r.announcements || [];
          annUnread.value = r.unread || 0;
          /* popup 模式的未读公告登录后弹一次。本会话内弹过的记在 shown 里,
             不重复入队 —— 面板每次打开都会重取公告,否则同一条会反复弹。 */
          (anns.value || []).forEach((a) => {
            if (a.notify_mode === "popup" && a.unread && !shown.has(a.id)) {
              shown.add(a.id);
              popQueue.value.push(a);
            }
          });
          if (!popup.value && popQueue.value.length)
            popup.value = popQueue.value.shift();
        }).catch(() => {});
      };
      /* ids=null 表示全部已读。标完重取,让 unread 与红点由后端口径决定。 */
      const markRead = (ids) => {
        if (!token()) return;
        api("POST", "/announcements/read", { ids: ids })
          .then(loadAnn).catch(() => {});
      };
      const closePopup = () => {
        const cur = popup.value;
        popup.value = null;
        if (cur) markRead([cur.id]);
        /* 队列里还有就接着弹,但等当前这个 modal 收完动画,避免两个叠在一起。 */
        if (popQueue.value.length)
          setTimeout(() => { popup.value = popQueue.value.shift(); }, 320);
      };
      watch(isAuth, (v) => { if (!v) { loadMe(); loadAnn(); } });

      /* 充值/兑码后余额会变,切到概览或钱包时顺手刷新顶栏。 */
      watch(path, (v) => {
        if (!token() || isAuth.value) return;
        if (v === "dashboard" || v === "wallet") loadMe();
      });
      /* 资料页改了昵称或头像要立刻反映到顶栏。用事件而不是让资料页直接改
         App 的 me:资料页是路由组件,拿不到外壳的 ref,走事件两边解耦。 */
      window.addEventListener("bitapi:me-changed", loadMe);
      /* 签到会同时改顶栏余额和网关可用余额,概览页发事件让外壳立刻重取。 */
      window.addEventListener("bitapi:balance-changed", loadMe);
      /* 管理台增删公告后顶栏铃铛要跟着变,同一套事件机制。 */
      window.addEventListener("bitapi:announcements-changed", loadAnn);

      watchEffect(() => {
        localStorage.setItem("bitapi_sider", collapsed.value ? "1" : "0");
      });

      /* 未登录只留 OPEN 里那几页:其余点进去都会被守卫弹回登录,
         摆在菜单里等于给一排死链。 */
      const navOptions = computed(() => NAV
        .filter((n) => (authed.value || OPEN[n[0]]) &&
          (n[0] !== "admin" || (me.value && me.value.role === "admin")))
        .map((n) => ({ key: n[0], label: n[1],
          icon: () => ic(n[2], 18, undefined, 1.8) })));

      const logout = () => {
        clearToken();
        me.value = null;
        bill.value = null;
        anns.value = [];
        annUnread.value = 0;
        popQueue.value = [];
        popup.value = null;
        shown.clear();
        location.hash = "#/login";
        msg.success("已退出登录");
      };

      /* 顶栏账号菜单:头像 + 昵称(缺省回落邮箱前缀)+ 下拉。原先把邮箱、角色、
         分组、策略四个 tag 平铺在标题右侧,邮箱一长就把主题开关挤到换行;
         身份信息收进下拉后顶栏只留「余额 + 主题 + 账号」三件。 */
      const shortName = computed(() => {
        const m = me.value || {};
        if (m.display_name) return m.display_name;
        const em = m.email || "";
        return em ? em.split("@")[0] : "未登录";
      });
      const initial = computed(() => (shortName.value[0] || "?").toUpperCase());
      const avatar = computed(() => (me.value && me.value.avatar) || "");
      const roleText = computed(() => (me.value && me.value.role === "admin"
        ? "管理员" : "普通用户"));
      /* 余额胶囊只要 /billing 回来了就挂 —— 余额与计费策略无关,免费组也能有余额。
         额度胶囊自己判断该不该出现(见 QuotaMini 的 show)。 */
      const showBalance = computed(() => !!bill.value);

      const userMenu = computed(() => {
        const items = [
          { key: "head", type: "render", render: () => h("div", { style: {
            padding: "11px 14px 9px", minWidth: "204px" } }, [
            h("div", { style: { fontSize: "13.5px", fontWeight: 650 } },
              shortName.value),
            h("div", { style: { fontSize: "11.5px", opacity: 0.55,
              marginTop: "2px" } }, (me.value && me.value.email) || ""),            h(naive.NSpace, { size: 5, style: { marginTop: "8px" } }, () => [
              me.value && me.value.role === "admin"
                ? tag("warning", "管理员") : tag("default", "普通用户"),
              bill.value && bill.value.group
                ? tag("info", bill.value.group) : null,
              bill.value
                ? tag("success", POLICY[bill.value.policy] || bill.value.policy)
                : null,
            ].filter(Boolean)),
          ]) },
          { key: "d1", type: "divider" },
          { key: "profile", label: "个人资料",
            icon: () => ic("user", 16, undefined, 1.8) },
          { key: "keys", label: "API 密钥",
            icon: () => ic("key", 16, undefined, 1.8) },
        ];
        if (me.value && me.value.role === "admin")
          items.push({ key: "admin", label: "管理控制台",
            icon: () => ic("gear", 16, undefined, 1.8) });
        items.push({ key: "d2", type: "divider" });
        items.push({ key: "logout",
          label: () => h("span", { style: { color: "#b91c1c" } }, "退出登录"),
          icon: () => ic("logout", 16, "#b91c1c", 1.8) });
        return items;
      });
      const onUserMenu = (key) => {
        if (key === "logout") return logout();
        location.hash = "#/" + key;
      };

      guard();
      loadMe();
      loadAnn();

      return { route, path, isAuth, page, meta, me, bill, anns, annUnread,
        popup, collapsed, dark, authed, narrow, drawer, siderCollapsed, setSider,
        theme, themeOverrides, navOptions, logout, POLICY, denied, loadAnn,
        markRead, closePopup,
        shortName, initial, avatar, roleText, showBalance, userMenu, onUserMenu,
        fmt: P.fmt, ic: ic,
        zhCN: naive.zhCN, dateZhCN: naive.dateZhCN,
        /* 窄屏点导航后要把抽屉收掉,否则它盖着刚跳过去的页面。 */
        nav: (v) => { drawer.value = false; location.hash = "#/" + v; } };
    },
    template: `
<n-config-provider :theme="theme" :theme-overrides="themeOverrides"
  :locale="zhCN" :date-locale="dateZhCN">
<n-global-style/>
<template v-if="isAuth">
  <CommunityCallback v-if="path === 'community-callback'"/>
  <!-- key 按路径:forgot → reset 是同一个组件换 mode,不重建的话表单里的
       邮箱框和上一步的报错会原地留下来。 -->
  <Recover v-else-if="path === 'forgot' || path === 'reset'" :key="path" :mode="path"
    :token="route.query.get('token') || ''"/>
  <Auth v-else :mode="path" :ref0="route.query.get('ref') || ''"/>
</template>
<n-layout v-else has-sider position="absolute">
  <!-- 窄屏:transform 模式 + collapsed-width 0 + absolute,侧栏滑出视野而不占位,
       盖在正文上当抽屉;宽屏保持原来的 width 模式(收起还留 64px 图标条)。 -->
  <n-layout-sider bordered :collapse-mode="narrow ? 'transform' : 'width'"
    :collapsed-width="narrow ? 0 : 64" :width="234"
    :collapsed="siderCollapsed" :show-trigger="!narrow"
    :position="narrow ? 'absolute' : 'static'"
    :native-scrollbar="false" :style="narrow ? 'z-index:3' : ''"
    @collapse="setSider(true)" @expand="setSider(false)">
    <div class="brand">
      <img class="brandmark" src="/static/bit-api-icon-180.png" alt="">
      <span v-if="!siderCollapsed">bit-api</span>
    </div>
    <n-menu :value="path" :options="navOptions" :collapsed="siderCollapsed"
      :collapsed-width="64" :collapsed-icon-size="20" :indent="20"
      @update:value="nav"/>
  </n-layout-sider>
  <!-- 抽屉展开时点旁边收起。手机上没有「点空白关掉」会很难受。 -->
  <div v-if="narrow && drawer" class="sidermask" @click="setSider(true)"></div>

  <n-layout-content :native-scrollbar="false"
    :content-style="narrow ? 'padding:14px 13px 44px' : 'padding:24px 30px 60px'">
    <div class="pagehead">
      <div class="headleft">
        <n-button v-if="narrow" class="hamb" quaternary circle size="small"
          aria-label="打开导航" @click="setSider(false)">
          <Ic name="menu" :size="19"/>
        </n-button>
        <div class="headtitle">
          <h2>{{ meta.title }}</h2>
          <p class="sub">{{ meta.sub }}</p>
        </div>
      </div>
      <n-space class="headright" align="center" :size="10">
        <!-- 顶栏右侧顺序:铃铛 → 主题 → 额度 → 余额 → 账号。
             铃铛在最左、余额紧挨账号,与参考图一致。
             未登录时铃铛与账号入口都没有意义(公告要登录才拉、账号菜单里
             全是要登录的项),换成登录/注册两颗按钮。 -->
        <Bell v-if="authed" :items="anns" :unread="annUnread"
          @refresh="loadAnn" @read="markRead"/>
        <n-tooltip placement="bottom">
          <template #trigger>
            <n-button quaternary circle size="small" @click="dark = !dark"
              :aria-label="dark ? '切到亮色' : '切到暗色'">
              <Ic :name="dark ? 'sun' : 'moon'" :size="17"/>
            </n-button>
          </template>
          {{ dark ? '切到亮色主题' : '切到暗色主题' }}
        </n-tooltip>
        <QuotaMini v-if="bill" :bill="bill" @nav="nav"/>
        <Balance v-if="showBalance" :bill="bill" @nav="nav"/>
        <n-dropdown v-if="authed" trigger="click" :options="userMenu"
          placement="bottom-end" @select="onUserMenu">
          <div class="usertrig">
            <Avatar :src="avatar" :text="initial" :size="28"/>
            <div v-if="me" class="uinfo">
              <div class="uname">{{ shortName }}</div>
              <div class="urole">{{ roleText }}</div>
            </div>
            <Ic name="chevronDown" :size="14"/>
          </div>
        </n-dropdown>
        <n-space v-else :size="7" align="center">
          <n-button size="small" quaternary @click="nav('login')">登录</n-button>
          <n-button size="small" type="primary" @click="nav('register')">
            注册</n-button>
        </n-space>
      </n-space>
    </div>
    <n-result v-if="denied" status="403" title="没有权限"
      description="管理控制台仅管理员可见。">
      <template #footer><n-button @click="nav('dashboard')">返回概览
      </n-button></template>
    </n-result>
    <component v-else :is="page" :key="path"/>
  </n-layout-content>
  <AnnPopup v-if="popup" :item="popup" @close="closePopup"/>
</n-layout>
</n-config-provider>`,
  };

  createApp(App).use(naive).mount("#app");

})();
