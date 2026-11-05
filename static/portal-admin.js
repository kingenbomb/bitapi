/* 管理控制台:站点设置 / 套餐分组 / 模型定价 / 用户 / 兑换码与订单。
   写操作全部走真 /api/admin/*。 */
(function () {
  "use strict";
  const { h, ref, reactive, computed, watch } = Vue;
  const P = window.BitPortal;
  const { api, qs, download, msg, dlg, copy, nf, kf, usd, fmt, MONO, absTime,
    relTime, st, POLICY, MODE, tag, money, reasonOf, modelChip, StatCard, Pills,
    BarChart, Donut, C, CT, stack, box, ic, tipRow, wrapTip,
    tpsOf, normUsage, usageColumns, END_TEXT, prov, BASE,
    planDur, planKind } = P;
  const { Load, useFetch } = window.BitPortalPages;

  const MONO_ATTR = { style: MONO };
  const muted = (t) => h(naive.NText, { depth: 3 }, () => t);
  const px = (v) => (v == null ? muted("—")
    : h("span", MONO_ATTR, "$" + Number(v).toFixed(4)));
  /* 表单里 0 与「不填」语义不同:长档 5 项留空表示该项回落普通价,不能补 0。 */
  const orNull = (v) => (v === null || v === undefined || v === "" ? null : Number(v));

  const POLICY_OPTS = ["balance", "quota", "free"].map(
    (v) => ({ label: POLICY[v] + "(" + v + ")", value: v }));
  const MODE_OPTS = ["token", "per_request", "free"].map(
    (v) => ({ label: MODE[v] + "(" + v + ")", value: v }));

  const AdminIntro = {
    props: ["title", "note", "eyebrow"],
    setup(props, { slots }) {
      return () => h("header", { class: "console-hero" }, [
        h("div", { class: "console-hero-copy" }, [
          h("div", { class: "console-eyebrow" }, props.eyebrow || "站点管理"),
          h("h1", { class: "console-hero-title" }, h(P.PageTitle, { text: props.title })),
          h("p", { class: "console-hero-note" }, props.note),
        ]),
        slots.default ? h("div", { class: "console-hero-actions" }, slots.default()) : null,
      ]);
    },
  };

  /* ---------------- tab 1 站点设置 ---------------- */

  const Settings = {
    components: { Load, AdminIntro },
    setup() {
      const section = ref("registration");
      const sections = [
        { key: "registration", label: "注册与分组", note: "新用户从哪里开始" },
        { key: "rewards", label: "签到奖励", note: "每日领取与入账" },
        { key: "invites", label: "邀请奖励", note: "有效注册后自动到账" },
        { key: "channels", label: "渠道开关", note: "控制模型对外可用性" },
        { key: "payment", label: "支付与充值", note: "渠道、汇率与商户" },
        { key: "mail", label: "邮件通知", note: "发信配置与测试" },
        { key: "system", label: "系统与备份", note: "运行来源与数据副本" },
      ];
      const cfg = ref({});
      const groups = ref([]);
      const chans = ref([]);
      const form = reactive({ require_invite: false, default_group: null,
        community_min_level: null });
      const levelOptions = Array.from({ length: 7 }, (_, level) => ({
        label: level === 0 ? "Lv0 及以上 · 所有有效等级" : `Lv${level} 及以上`,
        value: level,
      }));
      const levelDirty = computed(() => form.community_min_level !==
        cfg.value.registration_policy?.community_min_level);
      /* 支付设置单独一份 form:它的 dirty 判定要跟注册设置分开,
         否则改了汇率会让「注册与默认分组」那张卡也亮起未保存。
         epay_key 是只写字段 —— 服务端不回明文(只回末 4 位),
         所以留空表示「不改」,不能拿它跟 cfg 比 dirty。 */
      const pay = reactive({ payment_providers: [], min_topup: null,
        site_url: "", epay_api_url: "", epay_pid: "", epay_usd_rate: null,
        epay_key: "", baipiao_pid: "", baipiao_key: "", mao_per_usd: null });
      const checkinForm = reactive({ min: null, max: null });
      const inviteForm = reactive({ enabled: false, amount: null, max_invitees: null });
      const previewInvitees = ref(10);
      const savingInvite = ref(false);
      /* 共享 Key 奖励。channels 留空 = 所有 key 池型对话渠道都收。 */
      const shareForm = reactive({ enabled: false, amount: null, channels: [], max_per_user: null });
      const savingShare = ref(false);
      /* 邮件设置同支付一样单独一份 form;smtp_pass 只写不读,留空 = 不改。 */
      const mail = reactive({ smtp_host: "", smtp_port: null, smtp_user: "",
        smtp_pass: "", smtp_from: "", smtp_from_name: "", smtp_security: "ssl",
        site_name: "" });
      const mailTestTo = ref("");
      const saving = ref(false);
      const savingPay = ref(false);
      const savingCheckin = ref(false);
      const savingMail = ref(false);
      const testingMail = ref(false);
      const backup = ref({});
      const backingUp = ref(false);
      const s = useFetch(() => Promise.all([api("GET", "/admin/settings"),
        api("GET", "/admin/groups"),
        api("GET", "/admin/channels"),
        api("GET", "/admin/backup")]).then((r) => {
        cfg.value = r[0];
        groups.value = r[1].groups || [];
        chans.value = r[2].channels || [];
        backup.value = r[3] || {};
        form.require_invite = !!r[0].require_invite;
        form.default_group = r[0].default_group;
        form.community_min_level = r[0].registration_policy.community_min_level;
        pay.payment_providers = (r[0].payment_providers || []).slice();
        pay.min_topup = r[0].min_topup;
        pay.site_url = r[0].site_url || "";
        pay.epay_api_url = r[0].epay_api_url || "";
        pay.epay_pid = r[0].epay_pid || "";
        pay.epay_usd_rate = r[0].epay_usd_rate;
        pay.epay_key = "";              // 永不回填,留空=不改
        pay.baipiao_pid = r[0].baipiao_pid || "";
        pay.baipiao_key = "";
        pay.mao_per_usd = r[0].mao_per_usd;
        checkinForm.min = r[0].checkin_min;
        checkinForm.max = r[0].checkin_max;
        Object.assign(inviteForm, r[0].invite_rewards);
        Object.assign(shareForm, r[0].share_rewards);
        shareForm.channels = ((r[0].share_rewards || {}).channels || []).slice();
        mail.smtp_host = r[0].smtp_host || "";
        mail.smtp_port = r[0].smtp_port;
        mail.smtp_user = r[0].smtp_user || "";
        mail.smtp_pass = "";
        mail.smtp_from = r[0].smtp_from || "";
        mail.smtp_from_name = r[0].smtp_from_name || "";
        mail.smtp_security = r[0].smtp_security || "ssl";
        mail.site_name = r[0].site_name || "";
      }));
      const gopts = computed(() => groups.value.map(
        (g) => ({ label: g.name, value: g.name })));
      /* 渠道下拉只列服务端真正注册成功的,且不含 mock ——
         mock 不验签也不比金额,能从网页勾上就等于开一个免费充值口(服务端也硬拒)。 */
      const popts = computed(() => (cfg.value.registered_providers || [])
        .filter((n) => n !== "mock").map((n) => ({
          label: n === "baipiao" ? "白嫖社区毛支付" : n, value: n })));
      const cat = computed(() => cfg.value.pricing_catalog || {});
      const channels = computed(() => chans.value || []);
      const busyCh = ref("");
      /* 渠道开关是「一次提交一份完整状态」:后端收的是下线清单全量,不是增删一个。
         上线直接提交,下线先确认 —— 那一下会让正在调这些模型的客户端立刻拿到
         404,而管理员在这个页面上看不到有谁在调。 */
      const setChannel = (name, on) => {
        const off = new Set(cfg.value.disabled_channels || []);
        if (on) off.delete(name);
        else off.add(name);
        const apply = () => {
          busyCh.value = name;
          return api("PATCH", "/admin/settings",
                     { disabled_channels: Array.from(off) })
            .then(() => {
              msg.success(name + (on ? " 已上线" : " 已下线"));
              return s.reload();
            })
            .catch((e) => msg.error(e.message))
            .then(() => { busyCh.value = ""; });
        };
        if (on) return apply();
        dlg.warning({
          title: "下线渠道「" + name + "」",
          content: "它的模型会立刻从模型清单、模型广场和 /v1/* 路由上一起消失,"
            + "正在调用这些模型的客户端会收到「未知模型」。"
            + "号池、巡检与账号都不受影响,随时能上回来。",
          positiveText: "下线", negativeText: "取消",
          onPositiveClick: apply,
        });
      };
      const src = (k) => (cfg.value.from_db || {})[k] ? "已覆盖" : "来自环境变量";
      /* 只有被库覆盖过的项才给「恢复默认」—— 没覆盖时那个动作是空操作,
         摆在界面上等于让管理员去点一个什么都不会变的按钮。 */
      const isDb = (k) => !!(cfg.value.from_db || {})[k];
      const dirty = computed(() => !!cfg.value.defaults &&
        (form.require_invite !== !!cfg.value.require_invite ||
          form.default_group !== cfg.value.default_group || levelDirty.value));
      const payDirty = computed(() => {
        const c = cfg.value;
        if (!c.defaults) return false;
        return !!pay.epay_key
          || !!pay.baipiao_key
          || pay.baipiao_pid !== (c.baipiao_pid || "")
          || pay.mao_per_usd !== c.mao_per_usd
          || pay.min_topup !== c.min_topup
          || pay.epay_usd_rate !== c.epay_usd_rate
          || pay.site_url !== (c.site_url || "")
          || pay.epay_api_url !== (c.epay_api_url || "")
          || pay.epay_pid !== (c.epay_pid || "")
          || pay.payment_providers.join(",") !== (c.payment_providers || []).join(",");
      });
      const checkinDirty = computed(() => !!cfg.value.defaults &&
        (checkinForm.min !== cfg.value.checkin_min ||
          checkinForm.max !== cfg.value.checkin_max));
      const mailDirty = computed(() => {
        const c = cfg.value;
        if (!c.defaults) return false;
        return !!mail.smtp_pass
          || mail.smtp_host !== (c.smtp_host || "")
          || mail.smtp_port !== c.smtp_port
          || mail.smtp_user !== (c.smtp_user || "")
          || mail.smtp_from !== (c.smtp_from || "")
          || mail.smtp_from_name !== (c.smtp_from_name || "")
          || mail.smtp_security !== (c.smtp_security || "ssl")
          || mail.site_name !== (c.site_name || "");
      });
      const inviteDirty = computed(() => !!cfg.value.invite_rewards &&
        Object.keys(inviteForm).some((key) => inviteForm[key] !== cfg.value.invite_rewards[key]));
      const inviteError = computed(() => {
        if (inviteForm.amount == null || inviteForm.max_invitees == null) return "奖励金额和人数上限都不能为空";
        if (inviteForm.enabled && inviteForm.amount <= 0) return "开启奖励前，请填写大于 0 的每人奖励";
        return "";
      });
      const invitePreviewCount = computed(() => !inviteForm.enabled || inviteError.value ? 0 :
        Math.min(previewInvitees.value || 0, inviteForm.max_invitees || Infinity));
      const invitePreviewCost = computed(() => invitePreviewCount.value * (inviteForm.amount || 0));
      const saveInvite = () => {
        if (inviteError.value) return msg.error(inviteError.value);
        const policy = Object.assign({}, inviteForm);
        dlg.warning({
          title: "保存邀请奖励规则",
          content: (policy.enabled ? "开启注册邀请奖励，每位有效新用户奖励邀请人 $" + Number(policy.amount).toFixed(4)
            + (policy.max_invitees ? "，每位邀请人最多奖励 " + policy.max_invitees + " 人。" : "，奖励人数不限。") : "关闭后续注册的邀请奖励。")
            + "保存后立即对后续注册生效，已有入账保留，不补发历史邀请。",
          positiveText: "保存并生效", negativeText: "继续编辑",
          onPositiveClick: () => {
            savingInvite.value = true;
            return api("PATCH", "/admin/settings", { invite_rewards: policy })
              .then(() => { msg.success("邀请奖励规则已保存并生效"); return s.reload(); })
              .catch((e) => { msg.error(e.message); return false; })
              .finally(() => { savingInvite.value = false; });
          },
        });
      };
      /* 共享 Key:奖励金额、允许渠道、每人上限。 */
      const shareDirty = computed(() => {
        const c = cfg.value.share_rewards;
        if (!c) return false;
        const same = (a, b) => (a || []).slice().sort().join(",") === (b || []).slice().sort().join(",");
        return shareForm.enabled !== c.enabled
          || shareForm.amount !== c.amount
          || shareForm.max_per_user !== c.max_per_user
          || !same(shareForm.channels, c.channels);
      });
      const shareError = computed(() => {
        if (shareForm.amount == null || shareForm.max_per_user == null)
          return "每把奖励与每人上限都不能为空";
        if (shareForm.amount < 0) return "每把奖励不能为负";
        if (shareForm.amount > 1000) return "每把奖励不能超过 1000 美元";
        if (shareForm.enabled && shareForm.amount <= 0) return "开启共享前，请填写大于 0 的每把奖励";
        return "";
      });
      const shareChOptions = computed(() =>
        channels.value.map((c) => ({ label: c.name, value: c.name })));
      const saveShare = () => {
        if (shareError.value) return msg.error(shareError.value);
        const policy = { enabled: shareForm.enabled, amount: shareForm.amount,
          channels: (shareForm.channels || []).slice(), max_per_user: shareForm.max_per_user };
        dlg.warning({
          title: "保存共享 Key 规则",
          content: (policy.enabled
            ? "开启共享 Key：用户上传有效 key 后，每把奖励 $" + Number(policy.amount).toFixed(4)
              + (policy.channels.length ? "，仅收 " + policy.channels.join("、")
                : "，所有 key 池型对话渠道都收")
              + (policy.max_per_user ? "，每人最多 " + policy.max_per_user + " 把。"
                : "，每人把数不限。")
            : "关闭共享 Key：之后不再收新 key，已入库的号与已发出的奖励保留。"),
          positiveText: "保存并生效", negativeText: "继续编辑",
          onPositiveClick: () => {
            savingShare.value = true;
            return api("PATCH", "/admin/settings", { share_rewards: policy })
              .then(() => { msg.success("共享 Key 规则已保存并生效"); return s.reload(); })
              .catch((e) => { msg.error(e.message); return false; })
              .finally(() => { savingShare.value = false; });
          },
        });
      };
      const saveMail = () => {
        if (mail.smtp_port === null || mail.smtp_port === undefined) {
          msg.error("SMTP 端口不能留空 —— 要回到环境变量默认值,点该项下方的「恢复默认」");
          return;
        }
        const body = { smtp_host: mail.smtp_host, smtp_port: mail.smtp_port,
          smtp_user: mail.smtp_user, smtp_from: mail.smtp_from,
          smtp_from_name: mail.smtp_from_name, smtp_security: mail.smtp_security,
          site_name: mail.site_name };
        if (mail.smtp_pass) body.smtp_pass = mail.smtp_pass;   // 留空=不改
        savingMail.value = true;
        api("PATCH", "/admin/settings", body)
          .then(() => { msg.success("邮件设置已保存"); return s.reload(); })
          .catch((e) => msg.error(e.message))
          .then(() => { savingMail.value = false; });
      };
      /* 测试发信按「库里当前生效的」配置发,不是按表单里没保存的 —— 所以有未保存
         修改时先拦住,否则管理员会以为测的是刚填的那套。 */
      const testMail = () => {
        if (mailDirty.value) return msg.warning("先保存邮件设置,再发测试邮件");
        const to = mailTestTo.value.trim();
        if (!to) return msg.warning("填一个收件地址");
        testingMail.value = true;
        api("POST", "/admin/mail/test", { to: to })
          .then(() => msg.success("测试邮件已发往 " + to + ",去收件箱确认"))
          .catch((e) => msg.error(e.message))
          .then(() => { testingMail.value = false; });
      };
      const save = () => {
        const body = {};
        if (form.require_invite !== !!cfg.value.require_invite)
          body.require_invite = form.require_invite;
        if (form.default_group !== cfg.value.default_group)
          body.default_group = form.default_group;
        if (levelDirty.value) body.community_min_level = form.community_min_level;
        const apply = () => {
          saving.value = true;
          return api("PATCH", "/admin/settings", body)
            .then(() => { msg.success("注册设置已保存并生效"); return s.reload(); })
            .catch((e) => { msg.error(e.message); return false; })
            .finally(() => { saving.value = false; });
        };
        if (!levelDirty.value) return apply();
        dlg.warning({
          title: "调整社区注册门槛",
          content: `最低等级从 Lv${cfg.value.registration_policy.community_min_level} 改为 Lv${body.community_min_level}。`
            + `保存后仅 Lv${body.community_min_level} 及以上可通过社区新建账号，包括已授权但尚未完成的注册。`
            + "既有账号登录与绑定不受影响，其他身份校验保持不变。",
          positiveText: "保存并生效", negativeText: "继续编辑",
          onPositiveClick: apply,
        });
      };
      const saveCheckin = () => {
        if (checkinForm.min == null || checkinForm.max == null) {
          msg.error("签到最低额度和最高额度都不能为空");
          return;
        }
        if (Number(checkinForm.min) > Number(checkinForm.max)) {
          msg.error("签到最低额度不能高于最高额度");
          return;
        }
        savingCheckin.value = true;
        api("PATCH", "/admin/settings", {
          checkin_min: checkinForm.min, checkin_max: checkinForm.max,
        }).then(() => {
          msg.success("签到额度区间已保存");
          return s.reload();
        }).catch((e) => msg.error(e.message))
          .then(() => { savingCheckin.value = false; });
      };
      /* 数值项没有「空」这个合法值:清空输入框发出的是 null,而 null 既不是
         「设为空」也不是「恢复默认」,服务端直接拒。在这里先挡一层并指路,
         否则管理员清空一个框、点保存、看见成功提示,而值一刷新就回来了。 */
      const NUMERIC = { min_topup: "单笔最低充值", epay_usd_rate: "美元→人民币汇率",
        mao_per_usd: "每美元所需毛数" };
      const savePay = () => {
        const blank = Object.keys(NUMERIC).filter(
          (k) => pay[k] === null || pay[k] === undefined || pay[k] === "");
        if (blank.length) {
          msg.error(blank.map((k) => NUMERIC[k]).join("、")
            + " 不能留空 —— 要回到环境变量默认值,点该项下方的「恢复默认」");
          return;
        }
        const body = { payment_providers: pay.payment_providers,
          min_topup: pay.min_topup, site_url: pay.site_url,
          epay_api_url: pay.epay_api_url, epay_pid: pay.epay_pid,
          epay_usd_rate: pay.epay_usd_rate, baipiao_pid: pay.baipiao_pid,
          mao_per_usd: pay.mao_per_usd };
        if (pay.epay_key) body.epay_key = pay.epay_key;   // 留空=不改
        if (pay.baipiao_key) body.baipiao_key = pay.baipiao_key;
        savingPay.value = true;
        api("PATCH", "/admin/settings", body)
          .then((r) => {
            msg.success("支付设置已保存,当前生效渠道:"
              + ((r.active_providers || []).join(",") || "无"));
            return s.reload();
          })
          .catch((e) => msg.error(e.message))
          .then(() => { savingPay.value = false; });
      };
      /* 恢复环境变量默认值是删库里那一行,不是把输入框清空 ——
         清空是「明确设为空」(等于停用),两个意图不能混成一个。 */
      const resetKey = (k, label) => dlg.warning({
        title: "恢复环境变量默认值",
        content: `${label} 将回到部署时环境变量里的值。`,
        positiveText: "恢复", negativeText: "取消",
        onPositiveClick: () => api("DELETE", "/admin/settings/" + k)
          .then(() => { msg.success("已恢复默认"); return s.reload(); })
          .catch((e) => msg.error(e.message)),
      });
      const backupNow = () => {
        backingUp.value = true;
        api("POST", "/admin/backup")
          .then((r) => {
            msg.success("已备份 " + r.name + "(" + kf(r.size) + " 字节)");
            backup.value = r.status || backup.value;
          })
          .catch((e) => msg.error(e.message))
          .then(() => { backingUp.value = false; });
      };
      /* 上次备份的年龄超过一个周期半就该亮红:自动备份挂了三个月没人知道,
         正是这张卡要防的事。 */
      const backupStale = computed(() => {
        const b = backup.value;
        if (!b.interval) return false;
        if (!b.latest) return true;
        return (Date.now() / 1000 - b.latest.mtime) > b.interval * 1.5;
      });
      return Object.assign({ section, sections, cfg, form, pay, checkinForm, gopts, popts, cat,
        inviteForm, previewInvitees, savingInvite, inviteDirty, inviteError,
        invitePreviewCount, invitePreviewCost, saveInvite,
        shareForm, savingShare, shareDirty, shareError, shareChOptions, saveShare,
        levelOptions, levelDirty,
        dirty, payDirty, checkinDirty, saving, savingPay, savingCheckin,
        save, savePay, saveCheckin, resetKey, src, isDb,
        channels, busyCh, setChannel, backup, backingUp, backupNow, backupStale,
        mail, mailDirty, savingMail, saveMail, mailTestTo, testingMail, testMail,
        nf, kf, absTime, relTime, MONO }, s);
    },
    template: `
<Load :loading="loading" :error="error" :on-retry="reload">
<AdminIntro title="站点设置" note="按任务调整站点行为。每个分区独立保存，切换分区会保留当前填写内容。"/>
<div class="console-profile-layout">
  <nav class="console-anchor-nav" aria-label="设置分类">
    <button v-for="item in sections" :key="item.key" type="button"
      :class="{active: section === item.key}" :aria-current="section === item.key ? 'page' : undefined"
      @click="section = item.key">
      <span>{{ item.label }}<template v-if="(item.key === 'registration' && dirty) || (item.key === 'rewards' && checkinDirty) || (item.key === 'invites' && (inviteDirty || shareDirty)) || (item.key === 'payment' && payDirty) || (item.key === 'mail' && mailDirty)"> · 未保存</template></span>
      <small>{{ item.note }}</small>
    </button>
  </nav>
  <div class="admin-settings-content">
  <section v-show="section === 'registration'"><n-card size="small" title="注册与默认分组">
    <n-space vertical :size="18">
      <n-alert v-if="cfg.registration_policy && cfg.registration_policy.community_only" type="info" :bordered="false">
        仅支持白嫖社区授权注册，无需邀请码。下方门槛只影响社区新账号。
      </n-alert>
      <n-space v-else justify="space-between" align="center">
        <div>
          <div style="font-size:13px">邮箱注册需要邀请码</div>
          <n-text depth="3" style="font-size:11.5px">
            开启后新用户必须填码才能注册,环境变量默认
            {{ cfg.defaults && cfg.defaults.require_invite ? '开' : '关' }};
            仅接受「邀请码」页发放的一次性注册邀请码</n-text>
        </div>
        <n-switch v-model:value="form.require_invite"/>
      </n-space>
      <div class="admin-community-policy" v-if="cfg.registration_policy">
        <div class="admin-community-heading">
          <div>
            <div class="admin-community-title">社区最低注册等级</div>
            <p>选择门槛，保存后立即生效，无需重启。</p>
          </div>
          <span class="admin-community-live">当前生效 <strong>Lv{{ cfg.registration_policy.community_min_level }}+</strong></span>
        </div>
        <n-select v-model:value="form.community_min_level" :options="levelOptions"
          :disabled="saving" role="group" aria-label="社区最低注册等级"/>
        <div class="admin-community-preview" role="status" aria-live="polite" aria-atomic="true">
          <div class="admin-community-preview-heading">
            <strong>注册资格预览</strong>
            <span>{{ levelDirty ? '未保存' : '与当前规则一致' }}</span>
          </div>
          <ul class="admin-community-levels" aria-label="各社区等级的新账号注册资格">
            <li v-for="option in levelOptions" :key="option.value"
              :class="{eligible: option.value >= form.community_min_level}">
              <strong>Lv{{ option.value }}</strong>
              <span>{{ option.value >= form.community_min_level ? '可注册' : '未达标' }}</span>
            </li>
          </ul>
          <p>已有账号登录、绑定不受影响。缺失等级仍不可注册；QQ/Gmail 主邮箱、已验证与未封禁校验保持不变。</p>
        </div>
      </div>
      <n-form-item label="新用户默认分组" :show-feedback="false"
        label-placement="top">
        <n-select v-model:value="form.default_group" :options="gopts"
          placeholder="选择分组"/>
      </n-form-item>
      <n-text depth="3" style="font-size:11.5px">
        默认分组存在库里,优先级高于环境变量;环境变量默认分组为
        {{ cfg.defaults && cfg.defaults.default_group }}。
      </n-text>
    </n-space>
    <template #action><n-space align="center">
      <n-button type="primary" :disabled="!dirty || saving"
        :loading="saving" @click="save">保存并生效</n-button>
      <n-text v-if="dirty" depth="3" style="font-size:11.5px">有未保存的修改</n-text>
    </n-space></template>
  </n-card></section>

  <section v-show="section === 'system'"><n-card size="small" title="系统与备份">
    <n-descriptions :column="1" label-placement="left" size="small"
      :label-style="{opacity:.62,width:'88px'}">
      <n-descriptions-item label="价目表">
        <n-space :size="6" align="center">
          <n-tag size="small" round :bordered="false"
            :type="cat.entries ? 'success' : 'warning'">
            {{ cat.entries ? '已加载' : '未加载' }}</n-tag>
          <n-text v-if="cat.entries" :style="MONO" style="font-size:12px">
            {{ nf(cat.entries) }} 个模型</n-text>
        </n-space>
      </n-descriptions-item>
      <n-descriptions-item label="价目表来源">
        <n-text depth="3" style="font-size:11.5px;word-break:break-all">
          {{ cat.source || '—' }}</n-text>
      </n-descriptions-item>
      <n-descriptions-item label="最近加载">
        {{ cat.loaded_at ? relTime(cat.loaded_at) : '—' }}
      </n-descriptions-item>
      <n-descriptions-item label="支付渠道">
        <n-space :size="4">
          <n-tag v-for="p in (cfg.payment_providers || [])" :key="p" size="small"
            round :bordered="false" type="info" :style="MONO">{{ p }}</n-tag>
          <n-text v-if="!(cfg.payment_providers || []).length" depth="3">
            未启用</n-text>
        </n-space>
      </n-descriptions-item>
      <n-descriptions-item label="已启插件">
        <n-space :size="4">
          <n-tag v-for="p in (cfg.plugins || [])" :key="p" size="small" round
            :bordered="false" :style="MONO">{{ p }}</n-tag>
          <n-text v-if="!(cfg.plugins || []).length" depth="3">无</n-text>
        </n-space>
      </n-descriptions-item>
      <n-descriptions-item label="库备份">
        <n-space :size="6" align="center" wrap>
          <n-tag size="small" round :bordered="false"
            :type="backupStale ? 'error' : (backup.latest ? 'success' : 'default')">
            {{ !backup.interval ? '自动备份已关' : backup.latest
              ? (backupStale ? '已过期' : '正常') : '尚无备份' }}</n-tag>
          <n-text v-if="backup.latest" style="font-size:12px">
            上次 {{ relTime(backup.latest.mtime) }} · 共 {{ backup.count }} 份 ·
            {{ kf(backup.total_size || 0) }} 字节</n-text>
          <n-button size="tiny" secondary :loading="backingUp" @click="backupNow">
            立即备份</n-button>
        </n-space>
        <n-text depth="3" style="font-size:11px;display:block;margin-top:4px;word-break:break-all">
          {{ backup.dir }}
          <template v-if="backup.interval"> · 每 {{ Math.round(backup.interval / 3600) }} 小时一份,保留 {{ backup.keep }} 份</template>
        </n-text>
      </n-descriptions-item>
    </n-descriptions>
    <n-text depth="3" style="font-size:11.5px;display:block;margin-top:12px">
      支付渠道在「支付与充值」分区里改,存库即生效。插件仍由环境变量 PLUGINS 决定,
      控制台只读,改完重启进程生效。备份周期与份数由 BITAPI_BACKUP_INTERVAL /
      BITAPI_BACKUP_KEEP 决定,恢复 = 停服务、把备份文件改名放回库的位置。
    </n-text>
  </n-card></section>

  <section v-show="section === 'rewards'"><n-card size="small" title="每日签到奖励">
    <template #header-extra><n-text depth="3" style="font-size:11.5px">
      存库即生效 · 每人每天一次</n-text></template>
    <n-grid :cols="'1 700:2'" :x-gap="14" :y-gap="14" responsive="self">
      <n-gi>
        <n-form-item label="最低奖励(美元)" :show-feedback="false"
          label-placement="top">
          <n-input-number v-model:value="checkinForm.min" :min="0.0001" :max="100"
            :step="0.01" :precision="4" style="width:100%">
            <template #prefix>$</template></n-input-number>
        </n-form-item>
        <n-text depth="3" style="font-size:11.5px;display:block;margin-top:5px">
          {{ src('checkin_min') }} · 环境变量默认
          \${{ Number(cfg.defaults && cfg.defaults.checkin_min || 0).toFixed(4) }}
        </n-text>
      </n-gi>
      <n-gi>
        <n-form-item label="最高奖励(美元)" :show-feedback="false"
          label-placement="top">
          <n-input-number v-model:value="checkinForm.max" :min="0.0001" :max="100"
            :step="0.01" :precision="4" style="width:100%">
            <template #prefix>$</template></n-input-number>
        </n-form-item>
        <n-text depth="3" style="font-size:11.5px;display:block;margin-top:5px">
          {{ src('checkin_max') }} · 环境变量默认
          \${{ Number(cfg.defaults && cfg.defaults.checkin_max || 0).toFixed(4) }}
        </n-text>
      </n-gi>
    </n-grid>
    <n-text depth="3" style="font-size:11.5px;display:block;margin-top:12px">
      用户每天按 UTC+8 日期领取一次,系统在这个区间内按 $0.0001 一档随机入账,
      每笔都记录在余额流水中。
    </n-text>
    <template #action><n-space align="center">
      <n-button type="primary" size="small" :disabled="!checkinDirty"
        :loading="savingCheckin" @click="saveCheckin">保存签到额度</n-button>
      <n-text v-if="checkinDirty" depth="3" style="font-size:11.5px">
        有未保存的修改</n-text>
    </n-space></template>
  </n-card></section>

    <section v-show="section === 'invites'" class="console-page-stack console-invite-settings">
    <n-card size="small" title="注册邀请奖励">
      <p class="console-section-note">当前生效：{{ cfg.invite_rewards && cfg.invite_rewards.enabled ? '已开启，每人 $' + Number(cfg.invite_rewards.amount).toFixed(4) : '已关闭' }}。仅新用户通过有效注册、首次绑定邀请关系时发放。</p>
      <n-alert v-if="!(cfg.from_db || {}).invite_rewards && (cfg.plugins || []).includes('affiliate_fixed')" type="info" :bordered="false" style="margin:16px 0">当前沿用固定邀请插件。保存后替代旧插件的邀请奖励与新用户见面礼，仅按本页金额奖励邀请人。</n-alert>
      <n-form label-placement="top" class="console-invite-form">
        <n-form-item label="开启注册邀请奖励" :show-feedback="false">
          <n-switch v-model:value="inviteForm.enabled" aria-label="开启注册邀请奖励"/>
        </n-form-item>
        <n-grid :cols="'1 560:2'" :x-gap="20" :y-gap="16" responsive="self">
          <n-gi><n-form-item label="每人奖励（美元）" :show-feedback="false">
            <n-input-number v-model:value="inviteForm.amount" :min="0" :step="0.01" :precision="4" style="width:100%" :input-props="{'aria-label':'每人邀请奖励金额'}"><template #prefix>$</template></n-input-number>
          </n-form-item></n-gi>
          <n-gi><n-form-item label="每位邀请人的奖励人数上限" :show-feedback="false">
            <n-input-number v-model:value="inviteForm.max_invitees" :min="0" :precision="0" style="width:100%" :input-props="{'aria-label':'邀请奖励人数上限'}"/>
          </n-form-item><p class="console-section-note">0 表示不限；按历次已发奖人数累计。</p></n-gi>
        </n-grid>
      </n-form>
      <n-alert v-if="inviteError" type="warning" :bordered="false" style="margin-top:16px">{{ inviteError }}</n-alert>
      <div class="console-invite-preview">
        <h3 class="console-section-title">发奖预览</h3>
        <p class="console-section-note">假设同一邀请人此前未获注册奖励，新增以下数量的有效注册用户。</p>
        <n-form-item label="模拟新注册人数" label-placement="top" :show-feedback="false">
          <n-input-number v-model:value="previewInvitees" :min="1" :precision="0" :input-props="{'aria-label':'模拟新注册人数'}" style="max-width:240px"/>
        </n-form-item>
        <n-descriptions :column="1" label-placement="left">
          <n-descriptions-item label="预计发奖人数">{{ nf(invitePreviewCount) }} 人</n-descriptions-item>
          <n-descriptions-item label="预计奖励总额"><strong class="console-invite-cost">\${{ Number(invitePreviewCost).toFixed(4) }}</strong></n-descriptions-item>
          <n-descriptions-item label="判定依据">{{ !inviteForm.enabled ? '奖励开关关闭，不发奖' : inviteError || (inviteForm.max_invitees ? '按有效人数及每人上限发放' : '每位有效新用户均发放') }}</n-descriptions-item>
        </n-descriptions>
      </div>
      <p class="console-section-note">有效注册须通过站点既有邮箱验证或社区等级与邮箱核验。老账号登录、重复回调不会重复发奖；规则只用于后续注册，不补发历史邀请，也不改变已有邀请关系。</p>
      <template #action><n-space align="center">
        <n-button type="primary" :disabled="!inviteDirty || !!inviteError" :loading="savingInvite" @click="saveInvite">保存邀请奖励</n-button>
        <n-text v-if="inviteDirty" depth="3" style="font-size:12px">有未保存的修改</n-text>
      </n-space></template>
    </n-card>

    <n-card size="small" title="共享 Key 奖励">
      <p class="console-section-note">当前生效：{{ cfg.share_rewards && cfg.share_rewards.enabled ? '已开启，每把 $' + Number(cfg.share_rewards.amount).toFixed(4) : '已关闭' }}。用户上传有效 key 后,先拿这把 key 真发一次请求验活,通过才入库当号用并按把发奖。</p>
      <n-alert type="warning" :bordered="false" style="margin:16px 0">开启就等于对外开一个「交 key 换余额」的口子。同一把 key 全站只发一次奖;不同的人交同一把 key,只有先交的那个拿到钱。</n-alert>
      <n-form label-placement="top" class="console-invite-form">
        <n-form-item label="开启共享 Key" :show-feedback="false">
          <n-switch v-model:value="shareForm.enabled" aria-label="开启共享 Key"/>
        </n-form-item>
        <n-grid :cols="'1 560:2'" :x-gap="20" :y-gap="16" responsive="self">
          <n-gi><n-form-item label="每把 key 奖励（美元）" :show-feedback="false">
            <n-input-number v-model:value="shareForm.amount" :min="0" :max="1000" :step="0.5" :precision="4" style="width:100%" :input-props="{'aria-label':'每把 key 奖励金额'}"><template #prefix>$</template></n-input-number>
          </n-form-item></n-gi>
          <n-gi><n-form-item label="每人可拿奖励的 key 把数上限" :show-feedback="false">
            <n-input-number v-model:value="shareForm.max_per_user" :min="0" :precision="0" style="width:100%" :input-props="{'aria-label':'每人奖励把数上限'}"/>
          </n-form-item><p class="console-section-note">0 表示不限。</p></n-gi>
        </n-grid>
        <n-form-item label="允许共享的渠道" :show-feedback="false">
          <n-select v-model:value="shareForm.channels" multiple :options="shareChOptions"
            :placeholder="'留空 = 所有 key 池型对话渠道都收'" aria-label="允许共享的渠道"/>
        </n-form-item>
      </n-form>
      <n-alert v-if="shareError" type="warning" :bordered="false" style="margin-top:16px">{{ shareError }}</n-alert>
      <p class="console-section-note">只收声明了对话、且是 key 池型的渠道;转发型(自带号池)与纯向量渠道不在范围内。奖励走余额流水的 share_key 一笔,可在用户钱包里看到。</p>
      <template #action><n-space align="center">
        <n-button type="primary" :disabled="!shareDirty || !!shareError" :loading="savingShare" @click="saveShare">保存共享 Key 规则</n-button>
        <n-text v-if="shareDirty" depth="3" style="font-size:12px">有未保存的修改</n-text>
      </n-space></template>
    </n-card>
  </section>

  <section v-show="section === 'channels'"><n-card size="small" title="渠道上下线">
    <template #header-extra><n-text depth="3" style="font-size:11.5px">
      存库即生效,不必重启</n-text></template>
    <n-grid :cols="'1 560:2 1040:3'" :x-gap="12" :y-gap="12" responsive="self">
      <n-gi v-for="c in channels" :key="c.name">
        <div class="subpanel" style="padding:11px 13px">
          <n-space justify="space-between" align="center" :wrap="false">
            <div style="min-width:0">
              <div :style="[MONO, {fontSize:'13px',fontWeight:600}]">
                {{ c.name }}</div>
              <n-text depth="3" style="font-size:11.5px">
                {{ c.models.length }} 个模型 ·
                {{ c.kind === 'generation' ? '图/视频生成' : '对话' }}</n-text>
            </div>
            <n-switch size="small" :value="!c.disabled"
              :loading="busyCh === c.name"
              @update:value="(v) => setChannel(c.name, v)"/>
          </n-space>
        </div>
      </n-gi>
    </n-grid>
    <n-text depth="3" style="font-size:11.5px;display:block;margin-top:12px">
      下线只对用户生效:该渠道的模型从 /v1/models、模型广场与 /v1/* 路由上同时消失
      (调用返回「未知模型」),号池、巡检与管理面板照旧 —— 号还在,随时上回来。
      当前 {{ src('disabled_channels') }}
      <n-button v-if="isDb('disabled_channels')" text type="primary"
        style="font-size:11.5px"
        @click="resetKey('disabled_channels', '渠道开关')">恢复默认</n-button>
    </n-text>
  </n-card></section>

  <section v-show="section === 'payment'"><n-card size="small" title="支付设置">
    <template #header-extra><n-text depth="3" style="font-size:11.5px">
      存库即生效,不必重启</n-text></template>
    <n-grid :cols="'1 700:2'" :x-gap="14" :y-gap="14" responsive="self">
      <n-gi>
        <n-form-item label="启用的支付渠道" :show-feedback="false"
          label-placement="top">
          <n-select v-model:value="pay.payment_providers" multiple
            :options="popts" placeholder="留空 = 关闭支付"/>
        </n-form-item>
        <n-text depth="3" style="font-size:11.5px;display:block;margin-top:5px">
          {{ src('payment_providers') }}
          <n-button v-if="isDb('payment_providers')" text type="primary"
            style="font-size:11.5px"
            @click="resetKey('payment_providers', '启用的支付渠道')">恢复默认</n-button>
          · 留空即对外关闭充值。
          mock 渠道不验签也不比金额,不在此处提供。
        </n-text>
      </n-gi>
      <n-gi>
        <n-form-item label="单笔最低充值(美元)" :show-feedback="false"
          label-placement="top">
          <n-input-number v-model:value="pay.min_topup" :min="0.5" :max="10000"
            :precision="2" style="width:100%">
            <template #prefix>$</template></n-input-number>
        </n-form-item>
        <n-text depth="3" style="font-size:11.5px;display:block;margin-top:5px">
          {{ src('min_topup') }}
          <n-button v-if="isDb('min_topup')" text type="primary"
            style="font-size:11.5px"
            @click="resetKey('min_topup', '单笔最低充值')">恢复默认</n-button>
          · 服务端硬限,前端输入框只是软约束。清空不等于恢复默认。
        </n-text>
      </n-gi>
      <n-gi>
        <n-form-item label="站点公网地址" :show-feedback="false"
          label-placement="top">
          <n-input v-model:value="pay.site_url"
            placeholder="https://your-domain.com"/>
        </n-form-item>
        <n-text depth="3" style="font-size:11.5px;display:block;margin-top:5px">
          {{ src('site_url') }}
          <n-button v-if="isDb('site_url')" text type="primary"
            style="font-size:11.5px"
            @click="resetKey('site_url', '站点公网地址')">恢复默认</n-button>
          · 回调与跳回地址的根,必须外网可达、不带子路径。
          填错的话上游会把「已支付」投给别处。
        </n-text>
      </n-gi>
      <n-gi>
        <n-form-item label="美元→人民币汇率" :show-feedback="false"
          label-placement="top">
          <n-input-number v-model:value="pay.epay_usd_rate" :min="1" :max="50"
            :precision="4" style="width:100%"/>
        </n-form-item>
        <n-text depth="3" style="font-size:11.5px;display:block;margin-top:5px">
          {{ src('epay_usd_rate') }}
          <n-button v-if="isDb('epay_usd_rate')" text type="primary"
            style="font-size:11.5px"
            @click="resetKey('epay_usd_rate', '美元→人民币汇率')">恢复默认</n-button>
          · 只在下单那一刻用一次,应收金额冻进订单行,
          改了不影响在途单。把 7.2 写成 0.72 会让用户按一折付款,所以限制在 1~50。
        </n-text>
      </n-gi>
      <n-gi>
        <n-form-item label="易支付站点地址" :show-feedback="false"
          label-placement="top">
          <n-input v-model:value="pay.epay_api_url"
            placeholder="https://pay.example.com"/>
        </n-form-item>
        <n-text depth="3" style="font-size:11.5px;display:block;margin-top:5px">
          {{ src('epay_api_url') }}
          <n-button v-if="isDb('epay_api_url')" text type="primary"
            style="font-size:11.5px"
            @click="resetKey('epay_api_url', '易支付站点地址')">恢复默认</n-button>
          · 必须 https:查单请求会把商户密钥发到这里。
        </n-text>
      </n-gi>
      <n-gi>
        <n-form-item label="商户 PID" :show-feedback="false"
          label-placement="top">
          <n-input v-model:value="pay.epay_pid" placeholder="1001"/>
        </n-form-item>
        <n-text depth="3" style="font-size:11.5px;display:block;margin-top:5px">
          {{ src('epay_pid') }}
          <n-button v-if="isDb('epay_pid')" text type="primary"
            style="font-size:11.5px"
            @click="resetKey('epay_pid', '商户 PID')">恢复默认</n-button>
          · 与回调里的 pid 逐笔比对,不符即拒。
        </n-text>
      </n-gi>
      <n-gi :span="2">
        <n-form-item label="商户密钥" :show-feedback="false"
          label-placement="top">
          <n-input v-model:value="pay.epay_key" type="password"
            show-password-on="click"
            :placeholder="cfg.epay_key_set
              ? '已设置(末尾 ' + (cfg.epay_key_tail || '****') + '),留空表示不修改'
              : '未设置'"/>
        </n-form-item>
        <n-text depth="3" style="font-size:11.5px;display:block;margin-top:5px">
          {{ src('epay_key') }}
          <n-button v-if="isDb('epay_key')" text type="primary"
            style="font-size:11.5px"
            @click="resetKey('epay_key', '商户密钥')">恢复默认</n-button>
          · 只写不读:服务端不会把密钥回传浏览器,所以这里永远是空的,
          留空即表示本次不修改。改了密钥,在途已付订单的回调会验签失败 ——
          先在下方「订单」里查单收尾,再换密钥。
        </n-text>
      </n-gi>
    </n-grid>
    <n-divider>白嫖社区毛支付</n-divider>
    <n-grid :cols="'1 700:2'" :x-gap="14" :y-gap="14" responsive="self">
      <n-gi>
        <n-form-item label="白嫖商户 PID" :show-feedback="false" label-placement="top">
          <n-input v-model:value="pay.baipiao_pid" placeholder="社区开通的支付商户 ID"/>
        </n-form-item>
        <n-text depth="3" style="font-size:12px">{{ src('baipiao_pid') }}
          <n-button v-if="isDb('baipiao_pid')" text type="primary"
            @click="resetKey('baipiao_pid', '白嫖商户 PID')">恢复默认</n-button>
        </n-text>
      </n-gi>
      <n-gi>
        <n-form-item label="每 $1 API 余额所需毛数" :show-feedback="false" label-placement="top">
          <n-input-number v-model:value="pay.mao_per_usd" :min="0.01" :max="10000"
            :precision="4" style="width:100%"><template #suffix>毛</template></n-input-number>
        </n-form-item>
        <n-text depth="3" style="font-size:12px">默认 1 毛兑换 $1；填 2 则 2 毛兑换 $1。不足一毛向上取整。
          {{ src('mao_per_usd') }}
          <n-button v-if="isDb('mao_per_usd')" text type="primary"
            @click="resetKey('mao_per_usd', '每美元所需毛数')">恢复默认</n-button>
        </n-text>
      </n-gi>
      <n-gi>
        <n-form-item label="白嫖商户 KEY" :show-feedback="false" label-placement="top">
          <n-input v-model:value="pay.baipiao_key" type="password" show-password-on="click"
            :placeholder="cfg.baipiao_key_set ? '已设置，留空不修改' : '未设置'"/>
        </n-form-item>
        <n-text depth="3" style="font-size:12px">密钥只写不读；轮换前先核对未完成订单。
          {{ src('baipiao_key') }}
          <n-button v-if="isDb('baipiao_key')" text type="primary"
            @click="resetKey('baipiao_key', '白嫖商户 KEY')">恢复默认</n-button>
        </n-text>
      </n-gi>
      <n-gi><n-alert type="info" :bordered="false" style="font-size:12px">
        社区商户需启用并绑定收款账号，扣毛比例设为 1:1。本页比例只影响新订单。
        <a href="https://baipiao.org/docs/payment/" target="_blank" rel="noopener">查看接入文档</a>
        <div style="overflow-wrap:anywhere">通知：{{ pay.site_url }}/pay/notify/baipiao</div>
        <div style="overflow-wrap:anywhere">返回：{{ pay.site_url }}/pay/return/baipiao</div>
      </n-alert></n-gi>
    </n-grid>
    <template #action><n-space align="center">
      <n-button type="primary" size="small" :disabled="!payDirty"
        :loading="savingPay" @click="savePay">保存支付设置</n-button>
      <n-text v-if="payDirty" depth="3" style="font-size:11.5px">
        有未保存的修改</n-text>
      <n-text depth="3" style="font-size:11.5px">
        「恢复默认」在各项下方,只对已被库覆盖的项出现</n-text>
    </n-space></template>
  </n-card></section>

  <section v-show="section === 'mail'"><n-card size="small" title="邮件设置(SMTP)">
    <template #header-extra><n-space :size="8" align="center">
      <n-tag size="small" round :bordered="false"
        :type="cfg.mail_configured ? 'success' : 'warning'">
        {{ cfg.mail_configured ? '已配置' : '未配置' }}</n-tag>
      <n-text depth="3" style="font-size:11.5px">找回密码、邮箱验证都靠它;没配时找回密码入口会明说</n-text>
    </n-space></template>
    <n-grid :cols="'1 700:2 1100:4'" :x-gap="14" :y-gap="14" responsive="self">
      <n-gi :span="2">
        <n-form-item label="SMTP 主机" :show-feedback="false" label-placement="top">
          <n-input v-model:value="mail.smtp_host" placeholder="smtp.example.com"/>
        </n-form-item>
        <n-text depth="3" style="font-size:11.5px;display:block;margin-top:5px">
          {{ src('smtp_host') }}
          <n-button v-if="isDb('smtp_host')" text type="primary" style="font-size:11.5px"
            @click="resetKey('smtp_host', 'SMTP 主机')">恢复默认</n-button>
        </n-text>
      </n-gi>
      <n-gi>
        <n-form-item label="端口" :show-feedback="false" label-placement="top">
          <n-input-number v-model:value="mail.smtp_port" :min="1" :max="65535"
            :precision="0" style="width:100%"/>
        </n-form-item>
        <n-text depth="3" style="font-size:11.5px;display:block;margin-top:5px">
          {{ src('smtp_port') }}
          <n-button v-if="isDb('smtp_port')" text type="primary" style="font-size:11.5px"
            @click="resetKey('smtp_port', 'SMTP 端口')">恢复默认</n-button>
        </n-text>
      </n-gi>
      <n-gi>
        <n-form-item label="加密方式" :show-feedback="false" label-placement="top">
          <n-select v-model:value="mail.smtp_security" :options="[
            {label:'SSL(465)',value:'ssl'},{label:'STARTTLS(587)',value:'starttls'},
            {label:'不加密(25)',value:'none'}]"/>
        </n-form-item>
        <n-text depth="3" style="font-size:11.5px;display:block;margin-top:5px">
          {{ src('smtp_security') }}</n-text>
      </n-gi>
      <n-gi :span="2">
        <n-form-item label="登录用户名" :show-feedback="false" label-placement="top">
          <n-input v-model:value="mail.smtp_user" placeholder="通常就是发件邮箱;留空 = 不登录"/>
        </n-form-item>
        <n-text depth="3" style="font-size:11.5px;display:block;margin-top:5px">
          {{ src('smtp_user') }}</n-text>
      </n-gi>
      <n-gi :span="2">
        <n-form-item label="密码 / 授权码" :show-feedback="false" label-placement="top">
          <n-input v-model:value="mail.smtp_pass" type="password" show-password-on="click"
            :placeholder="cfg.smtp_pass_set ? '已设置,留空表示不修改' : '未设置'"/>
        </n-form-item>
        <n-text depth="3" style="font-size:11.5px;display:block;margin-top:5px">
          {{ src('smtp_pass') }}
          <n-button v-if="isDb('smtp_pass')" text type="primary" style="font-size:11.5px"
            @click="resetKey('smtp_pass', 'SMTP 密码')">恢复默认</n-button>
          · 只写不读。QQ / 163 这类邮箱填的是「授权码」不是登录密码。
        </n-text>
      </n-gi>
      <n-gi :span="2">
        <n-form-item label="发件地址" :show-feedback="false" label-placement="top">
          <n-input v-model:value="mail.smtp_from" placeholder="no-reply@example.com"/>
        </n-form-item>
        <n-text depth="3" style="font-size:11.5px;display:block;margin-top:5px">
          {{ src('smtp_from') }}
          <n-button v-if="isDb('smtp_from')" text type="primary" style="font-size:11.5px"
            @click="resetKey('smtp_from', '发件地址')">恢复默认</n-button>
          · 多数服务商要求它与登录账号一致。
        </n-text>
      </n-gi>
      <n-gi>
        <n-form-item label="发件人显示名" :show-feedback="false" label-placement="top">
          <n-input v-model:value="mail.smtp_from_name" placeholder="可空"/>
        </n-form-item>
      </n-gi>
      <n-gi>
        <n-form-item label="站点名(邮件标题用)" :show-feedback="false" label-placement="top">
          <n-input v-model:value="mail.site_name" placeholder="bit-api"/>
        </n-form-item>
        <n-text depth="3" style="font-size:11.5px;display:block;margin-top:5px">
          {{ src('site_name') }}</n-text>
      </n-gi>
    </n-grid>
    <template #action><n-space align="center" wrap>
      <n-button type="primary" size="small" :disabled="!mailDirty"
        :loading="savingMail" @click="saveMail">保存邮件设置</n-button>
      <n-text v-if="mailDirty" depth="3" style="font-size:11.5px">有未保存的修改</n-text>
      <n-input-group style="width:320px;max-width:100%">
        <n-input v-model:value="mailTestTo" size="small" placeholder="收件地址"
          @keyup.enter="testMail"/>
        <n-button size="small" secondary :loading="testingMail"
          :disabled="!cfg.mail_configured" @click="testMail">发测试邮件</n-button>
      </n-input-group>
      <n-text depth="3" style="font-size:11.5px">按已保存的配置发;失败会把 SMTP 的原话显示出来</n-text>
    </n-space></template>
  </n-card></section>
  </div>
</div>
</Load>`,
  };

  /* ---------------- tab 2 套餐分组 ---------------- */

  /* 时长预设。存库只存小时数,0 = 不限时(买了永久有效)。
     预设之外的时长直接填小时数,两个控件绑同一个字段。 */
  const DUR_PRESETS = [["小时卡", 1], ["6 小时", 6], ["天卡", 24], ["3 天", 72],
    ["周卡", 168], ["月卡", 720], ["不限时", 0]];

  const NEW_GROUP = () => ({ name: "", rate_multiplier: 1, models: ["*"],
    billing_policy: "free", concurrency_limit: 0, daily_limit: 0, weekly_limit: 0,
    monthly_limit: 0, limit_unit: "requests", is_default: 0,
    listed: 0, price: 0, duration_hours: 24, notes: "" });

  const Groups = {
    components: { Load, AdminIntro },
    setup() {
      const rows = ref([]);
      const chans = ref([]);
      const show = ref(false);
      const busy = ref(false);
      const g = reactive(NEW_GROUP());
      /* null = 新建,否则是正在改的那一行。一套表单两用,免得建组和改组
         两份几乎一样的字段各写一遍、加字段时漏掉一边。 */
      const editing = ref(null);
      const s = useFetch(() => Promise.all([
        api("GET", "/admin/groups"), api("GET", "/admin/channels"),
      ]).then((r) => {
        rows.value = r[0].groups || [];
        chans.value = r[1].channels || [];
      }));

      /* 可用模型的下拉:按渠道分组,每组顶上是「整个渠道」的通配(由服务端从
         模型名算公共前缀,算不出来的渠道就没有这一项
         vendor/model 形状)。仍然可以直接打字 —— 白名单收的是模式,不是清单,
         `gpt-*` 这种自定义写法必须留得下。 */
      const modelOpts = computed(() => {
        const out = [{ label: "*  (全部模型)", value: "*" }];
        chans.value.forEach((c) => {
          const kids = [];
          if (c.wildcard)
            kids.push({ label: c.wildcard + "  (整个渠道)", value: c.wildcard });
          (c.models || []).forEach((m) => kids.push({ label: m, value: m }));
          out.push({ type: "group", key: c.name, children: kids,
            label: c.name + " · " + (c.models || []).length + " 个模型"
              + (c.disabled ? " · 已下线" : "") });
        });
        return out;
      });

      const patch = (row, fields, okText) =>
        api("PATCH", "/admin/groups/" + row.id, fields)
          .then(() => { msg.success(okText || "已更新"); return s.reload(); })
          .catch((e) => { msg.error(e.message); return s.reload(); });

      const openNew = () => {
        Object.assign(g, NEW_GROUP());
        editing.value = null;
        show.value = true;
      };
      const openEdit = (row) => {
        Object.assign(g, NEW_GROUP(), {
          name: row.name,
          rate_multiplier: row.rate_multiplier || 1,
          models: (row.supported_models || []).slice(),
          billing_policy: row.billing_policy || "free",
          concurrency_limit: row.concurrency_limit || 0,
          daily_limit: row.daily_limit || 0,
          weekly_limit: row.weekly_limit || 0,
          monthly_limit: row.monthly_limit || 0,
          limit_unit: row.limit_unit || "requests",
          is_default: row.is_default || 0,
          listed: row.listed || 0,
          price: row.price || 0,
          duration_hours: row.duration_hours || 0,
          notes: row.notes || "",
        });
        editing.value = row;
        show.value = true;
      };

      const submit = () => {
        const body = {
          rate_multiplier: Number(g.rate_multiplier) || 1,
          supported_models: (g.models || []).map((x) => String(x).trim())
            .filter(Boolean),
          billing_policy: g.billing_policy,
          concurrency_limit: Number(g.concurrency_limit) || 0,
          daily_limit: Number(g.daily_limit) || 0,
          weekly_limit: Number(g.weekly_limit) || 0,
          monthly_limit: Number(g.monthly_limit) || 0,
          limit_unit: g.limit_unit,
          is_default: g.is_default ? 1 : 0,
          listed: g.listed ? 1 : 0,
          price: Number(g.price) || 0,
          duration_hours: Number(g.duration_hours) || 0,
          notes: (g.notes || "").trim(),
        };
        if (!editing.value && !g.name.trim()) return msg.warning("请填写分组名");
        /* 服务端也拦这一条(0 元套餐上架等于白送一个计费档),这里先拦一次是
           为了让提示落在填价那个框边上,而不是提交后弹一句英文 detail。 */
        if (body.listed && !(body.price > 0))
          return msg.warning("上架的套餐必须填售价");
        busy.value = true;
        const done = editing.value
          ? api("PATCH", "/admin/groups/" + editing.value.id, body)
          : api("POST", "/admin/groups",
                Object.assign({ name: g.name.trim() }, body));
        return done.then(() => {
          msg.success(editing.value ? "已保存" : "分组已创建");
          show.value = false;
          editing.value = null;
          Object.assign(g, NEW_GROUP());
          return s.reload();
        }).catch((e) => msg.error(e.message))
          .then(() => { busy.value = false; });
      };

      const del = (row) => dlg.warning({
        title: "删除分组「" + row.name + "」",
        content: "不可逆,而且会连带删掉这个分组的专属定价。"
          + "只是想停止售卖就下架,想让它对现有用户失效就停用 —— 那两条都能撤回。",
        positiveText: "删除", negativeText: "取消",
        onPositiveClick: () => api("DELETE", "/admin/groups/" + row.id)
          .then(() => { msg.success("分组已删除"); return s.reload(); })
          .catch((e) => msg.error(e.message)),
      });

      const cols = [
        { title: "分组", key: "name", minWidth: 150, fixed: "left",
          render: (r) => stack(h(naive.NSpace, { size: 5, align: "center" },
            () => [h("span", { style: { fontWeight: 600 } }, r.name),
              r.is_default ? tag("primary", "默认") : null,
              r.status === "active" ? null : tag("error", "已停用")]),
            "#" + r.id + " · 倍率 ×" + r.rate_multiplier) },
        { title: "计费策略", key: "billing_policy", width: 148,
          render: (r) => h(naive.NSelect, { size: "tiny",
            value: r.billing_policy || "free", options: POLICY_OPTS,
            consistentMenuWidth: false,
            onUpdateValue: (v) => patch(r, { billing_policy: v }, "计费策略已改为 " +
              (POLICY[v] || v)) }) },
        /* 上架开关直接放在表里:上/下架是最常动的一栏,不值得为它开弹窗。
           0 元档上架会被服务端 400 拦下,提示照原样弹出来。 */
        { title: "上架售卖", key: "listed", width: 176,
          render: (r) => h(naive.NSpace, { size: 8, align: "center" }, () => [
            h(naive.NSwitch, { size: "small", value: !!r.listed,
              onUpdateValue: (v) => patch(r, { listed: v ? 1 : 0 },
                v ? "已上架,用户端可购" : "已下架") }),
            r.listed
              ? stack(h("span", MONO_ATTR, "$" + Number(r.price || 0).toFixed(2)),
                  planKind(r.duration_hours) + " · " + planDur(r.duration_hours))
              : muted("未上架"),
          ]) },
        { title: "限额(日/周/月)", key: "limits", width: 168,
          render: (r) => stack(h("span", MONO_ATTR,
            [r.daily_limit || 0, r.weekly_limit || 0, r.monthly_limit || 0]
              .map((x) => (x ? nf(x) : "∞")).join(" / ")),
            "口径 " + (r.limit_unit === "tokens" ? "Token" : "请求次数")) },
        { title: "并发上限", key: "concurrency_limit", width: 98, align: "right",
          render: (r) => h("span", MONO_ATTR, r.concurrency_limit ? nf(r.concurrency_limit) : "∞") },
        { title: "可用模型", key: "supported_models", minWidth: 240,
          render: (r) => {
            const list = r.supported_models || [];
            if (!list.length) return muted("未配置(禁止全部)");
            return h(naive.NSpace, { size: 4 }, () => list.slice(0, 6).map(
              (m) => tag(m === "*" ? "success" : "default", m)).concat(
              list.length > 6 ? [muted("+" + (list.length - 6))] : [])); } },
        { title: "操作", key: "act", width: 238, fixed: "right",
          render: (r) => h(naive.NSpace, { size: 6 }, () => [
            h(naive.NButton, { size: "tiny", secondary: true,
              onClick: () => openEdit(r) }, () => "编辑"),
            r.is_default ? null : h(naive.NButton, { size: "tiny",
              secondary: true, onClick: () => patch(r, { is_default: 1 },
                "已设为默认分组") }, () => "设默认"),
            h(naive.NButton, { size: "tiny", secondary: true,
              type: r.status === "active" ? "warning" : "success",
              onClick: () => patch(r, { status: r.status === "active"
                ? "disabled" : "active" }) },
              () => (r.status === "active" ? "停用" : "启用")),
            r.is_default ? null : h(naive.NButton, { size: "tiny",
              quaternary: true, type: "error", onClick: () => del(r) },
              () => "删除"),
          ]) },
      ];
      return Object.assign({ rows, cols, show, busy, g, editing, submit,
        openNew, modelOpts, DUR_PRESETS, POLICY_OPTS, nf, planDur, planKind }, s);
    },
    template: `
<Load :loading="loading" :error="error" :on-retry="reload">
<AdminIntro title="套餐与分组" note="把模型范围、额度和售价组织成清晰的服务档位。现有用户与新购用户的影响在编辑时分别说明。">
  <n-button type="primary" @click="openNew">新建分组</n-button>
</AdminIntro>
<div class="console-resource-strip">
  <div class="console-inline-stat"><span class="console-inline-stat-value">{{ rows.length }}</span><span class="console-inline-stat-label">已配置分组</span></div>
  <div class="console-inline-stat"><span class="console-inline-stat-value">{{ rows.filter(r => r.listed).length }}</span><span class="console-inline-stat-label">上架售卖</span></div>
  <div class="console-inline-stat"><span class="console-inline-stat-value">{{ (rows.find(r => r.is_default) || {}).name || '—' }}</span><span class="console-inline-stat-label">默认标记 · 注册分组由站点设置控制</span></div>
</div>
<n-card class="console-table-card" size="small" title="当前分组">
  <n-data-table :columns="cols" :data="rows" size="small" :bordered="false"
    :single-line="false" :scroll-x="1300" :row-key="(r) => r.id"/>
  <n-text depth="3" style="font-size:11.5px;display:block;margin-top:10px">
    上架后这个分组就是用户端「计费与套餐」页里的一张时长卡:用户按售价从余额扣款、
    按时长生效,到期自动回落默认分组按量计费。分组名建好不改;删除只在没有用户挂着时可用,
    其余情况用下架(停止售卖)或停用(对现有用户失效)。
  </n-text>
</n-card>

<n-modal v-model:show="show" preset="card"
  :title="editing ? '编辑分组 · ' + g.name : '新建分组'"
  style="max-width:660px" :bordered="false">
  <n-grid :cols="'1 500:2'" :x-gap="12" :y-gap="12" responsive="self">
    <n-gi><n-form-item label="分组名" :show-feedback="false"
      label-placement="top"><n-input v-model:value="g.name" :disabled="!!editing"
      placeholder="day-card"/></n-form-item></n-gi>
    <n-gi><n-form-item label="计费策略" :show-feedback="false"
      label-placement="top"><n-select v-model:value="g.billing_policy"
      :options="POLICY_OPTS"/></n-form-item></n-gi>
    <n-gi :span="2"><n-form-item :show-feedback="false" label-placement="top"
      label="可用模型(按渠道分组;也可直接输入 prefix* 这类模式后回车)">
      <!-- 这一项必须关掉虚拟滚动:naive 的虚拟列表遇上「分组选项」会漏更新,
           往下滑的时候窗口顶部会挂着几行已经滑过去的旧行(实测:滚到 kogo 一带,
           顶上还有别的渠道,越滚越多)。同一份选项拍平了就没事,所以是分组
           那个形状踩的坑,不是项数多。关掉以后就是普通 DOM,滚动只是移容器。
           项数就两百上下,全渲染的代价可以忽略。 -->
      <n-select v-model:value="g.models" multiple filterable tag
        :options="modelOpts" max-tag-count="responsive" :virtual-scroll="false"
        placeholder="选 * 放开全部,或按渠道/单个模型挑"/>
    </n-form-item>
    <n-text depth="3" style="font-size:11px;display:block;margin-top:5px">
      空着 = 这个分组一个模型都不能调(不是「不限」)。
      「整个渠道」那一项是按该渠道现有模型名的公共前缀算的,
      以后新增的模型只要还带这个前缀就自动包含在内。
    </n-text></n-gi>
    <n-gi><n-form-item label="倍率" :show-feedback="false"
      label-placement="top"><n-input-number v-model:value="g.rate_multiplier"
      :min="0" :step="0.1" style="width:100%"/></n-form-item></n-gi>
    <n-gi><n-form-item label="每用户并发上限(0=不限)" :show-feedback="false"
      label-placement="top"><n-input-number v-model:value="g.concurrency_limit" :min="0"
      :precision="0" style="width:100%"/></n-form-item>
      <n-text depth="3" style="font-size:11px;display:block;margin-top:5px">
        同一用户的所有密钥与在线体验共用。流式请求结束后释放名额。
      </n-text></n-gi>
    <n-gi><n-form-item label="限额口径" :show-feedback="false"
      label-placement="top"><n-select v-model:value="g.limit_unit" :options="[
        {label:'请求次数',value:'requests'},{label:'Token',value:'tokens'}]"/>
    </n-form-item></n-gi>
    <n-gi><n-form-item label="日限额(0=不限)" :show-feedback="false"
      label-placement="top"><n-input-number v-model:value="g.daily_limit"
      :min="0" style="width:100%"/></n-form-item></n-gi>
    <n-gi><n-form-item label="周限额" :show-feedback="false"
      label-placement="top"><n-input-number v-model:value="g.weekly_limit"
      :min="0" style="width:100%"/></n-form-item></n-gi>
    <n-gi><n-form-item label="月限额" :show-feedback="false"
      label-placement="top"><n-input-number v-model:value="g.monthly_limit"
      :min="0" style="width:100%"/></n-form-item></n-gi>
    <n-gi :span="2"><n-checkbox :checked="!!g.is_default"
      @update:checked="(v) => g.is_default = v ? 1 : 0">
      设为新用户默认分组</n-checkbox></n-gi>
  </n-grid>

  <n-divider style="margin:16px 0 12px"><n-text depth="3"
    style="font-size:12px">上架售卖(时长卡)</n-text></n-divider>
  <n-space vertical :size="12">
    <n-checkbox :checked="!!g.listed"
      @update:checked="(v) => g.listed = v ? 1 : 0">
      放进用户端可购列表 —— 用户自助购买并切到这个档</n-checkbox>
    <n-grid :cols="'1 500:2'" :x-gap="12" :y-gap="12" responsive="self">
      <n-gi><n-form-item label="售价(美元,从余额扣)" :show-feedback="false"
        label-placement="top"><n-input-number v-model:value="g.price" :min="0"
        :precision="2" :disabled="!g.listed" style="width:100%">
        <template #prefix>$</template></n-input-number></n-form-item></n-gi>
      <n-gi><n-form-item :show-feedback="false" label-placement="top"
        :label="'有效时长:' + planKind(g.duration_hours) + ' · '
          + planDur(g.duration_hours)">
        <n-input-number v-model:value="g.duration_hours" :min="0"
        :disabled="!g.listed" style="width:100%">
        <template #suffix>小时</template></n-input-number></n-form-item></n-gi>
    </n-grid>
    <n-space :size="6">
      <n-button v-for="d in DUR_PRESETS" :key="d[1]" size="tiny"
        :secondary="g.duration_hours !== d[1]"
        :type="g.duration_hours === d[1] ? 'primary' : 'default'"
        :disabled="!g.listed" @click="g.duration_hours = d[1]">{{ d[0] }}</n-button>
    </n-space>
    <n-input v-model:value="g.notes" :disabled="!g.listed"
      placeholder="卖点文案,显示在用户端套餐卡上,如「不限量、10 并发、含 Claude 全系」"/>
  </n-space>
  <n-text depth="3" style="font-size:11.5px;display:block;margin-top:12px">
    限额只在「额度限额」策略下生效 —— 天卡/小时卡通常配这个策略加日限额,
    卖的就是这份额度;配「余额扣费」的档卖的是倍率与模型范围,用量仍按单价扣余额。
  </n-text>
  <div class="admin-impact" style="margin-top:16px">
    <template v-if="editing">修改计费策略、模型范围和限额，将影响使用此分组的后续请求。</template>
    <template v-else>创建后可将用户分配到此分组；新用户的注册分组由站点设置控制。</template>
    <template v-if="g.listed">上架后，用户可支付 \${{ Number(g.price || 0).toFixed(2) }} 购买 {{ planDur(g.duration_hours) }} 的使用时长。</template>
    <template v-else>当前不开放自助购买；已有用户的分组关系不会因下架而解除。</template>
  </div>
  <template #footer><n-space justify="end">
    <n-button size="small" @click="show = false">取消</n-button>
    <n-button size="small" type="primary" :loading="busy" @click="submit">
      {{ editing ? '保存' : '创建' }}</n-button>
  </n-space></template>
</n-modal>
</Load>`,
  };
  /* ---------------- tab 3 模型定价 ---------------- */

  const NEW_PRICE = () => ({ model_pattern: "", group_id: 0,
    billing_mode: "token", input_price: 0, output_price: 0,
    cache_read_price: 0, cache_write_price: 0, per_request_price: 0,
    long_threshold: 0, long_input_price: null, long_output_price: null,
    long_cache_read_price: null, long_cache_write_price: null, notes: "" });

  const Pricing = {
    components: { Load, AdminIntro },
    setup() {
      const rows = ref([]);
      const cat = ref({});
      const groups = ref([]);
      const p = reactive(NEW_PRICE());
      const busy = reactive({ save: false, seed: false, imp: false });
      const filter = ref("");
      const file = ref(null);
      const showEditor = ref(false);
      const openNew = () => { Object.assign(p, NEW_PRICE()); showEditor.value = true; };

      const s = useFetch(() => Promise.all([api("GET", "/admin/pricing"),
        api("GET", "/admin/groups")]).then((r) => {
        rows.value = r[0].pricing || [];
        cat.value = r[0].catalog || {};
        groups.value = r[1].groups || [];
      }));

      const gopts = computed(() => [{ label: "全局(所有分组)", value: 0 }].concat(
        groups.value.map((g) => ({ label: g.name, value: g.id }))));
      const gname = (id) => {
        if (!id) return null;
        const g = groups.value.filter((x) => x.id === id)[0];
        return g ? g.name : "#" + id;
      };
      const view = computed(() => {
        const q = filter.value.trim().toLowerCase();
        return q ? rows.value.filter((r) =>
          String(r.model_pattern).toLowerCase().indexOf(q) >= 0) : rows.value;
      });
      const longCount = computed(() =>
        rows.value.filter((r) => r.long_threshold).length);

      /* 编辑就是把行灌回表单:后端是 upsert,同 (pattern, group) 再存一次即覆盖。 */
      const edit = (r) => {
        Object.assign(p, NEW_PRICE());
        Object.keys(p).forEach((k) => {
          if (r[k] !== undefined) p[k] = r[k];
        });
        p.group_id = r.group_id || 0;
        p.notes = r.notes || "";
        showEditor.value = true;
      };

      const save = () => {
        if (!p.model_pattern.trim()) return msg.warning("请填写模型名");
        busy.save = true;
        api("POST", "/admin/pricing", {
          model_pattern: p.model_pattern.trim(),
          group_id: p.group_id ? p.group_id : null,
          billing_mode: p.billing_mode,
          input_price: Number(p.input_price) || 0,
          output_price: Number(p.output_price) || 0,
          cache_read_price: Number(p.cache_read_price) || 0,
          cache_write_price: Number(p.cache_write_price) || 0,
          per_request_price: Number(p.per_request_price) || 0,
          long_threshold: Number(p.long_threshold) || 0,
          long_input_price: orNull(p.long_input_price),
          long_output_price: orNull(p.long_output_price),
          long_cache_read_price: orNull(p.long_cache_read_price),
          long_cache_write_price: orNull(p.long_cache_write_price),
          notes: p.notes || null,
        }).then(() => {
          msg.success("已保存 " + p.model_pattern.trim());
          Object.assign(p, NEW_PRICE());
          showEditor.value = false;
          return s.reload();
        }).catch((e) => msg.error(e.message))
          .then(() => { busy.save = false; });
      };

      const del = (r) => dlg.warning({
        title: "删除定价",
        content: "删除 " + r.model_pattern + " 后,该模型将回落到内置价目表," +
          "价目表也没有则按免费计费。历史账单已冻结快照,不受影响。",
        positiveText: "删除", negativeText: "取消",
        onPositiveClick: () => api("DELETE", "/admin/pricing/" + r.id)
          .then(() => { msg.success("已删除"); return s.reload(); })
          .catch((e) => msg.error(e.message)),
      });

      const exportJson = () => {
        const payload = { pricing: rows.value.map((r) => {
          const o = {};
          ["model_pattern", "group_id", "billing_mode", "input_price",
            "output_price", "cache_read_price", "cache_write_price",
            "per_request_price", "long_threshold", "long_input_price",
            "long_output_price", "long_cache_read_price",
            "long_cache_write_price", "notes"].forEach((k) => {
            if (r[k] !== undefined) o[k] = r[k];
          });
          return o;
        }) };
        const url = URL.createObjectURL(new Blob(
          [JSON.stringify(payload, null, 2)], { type: "application/json" }));
        const a = document.createElement("a");
        a.href = url;
        a.download = "bitapi-pricing.json";
        a.click();
        URL.revokeObjectURL(url);
        msg.success("已导出 " + rows.value.length + " 条");
      };

      const doImport = (list, label) =>
        api("POST", "/admin/pricing/import", { pricing: list }).then((r) => {
          const skip = (r.skipped || []).length;
          msg.success(label + "已导入 " + r.imported + " 条" +
            (skip ? ",跳过 " + skip + " 条" : ""));
          return s.reload();
        }).catch((e) => msg.error(e.message));

      const seed = () => {
        busy.seed = true;
        fetch(BASE + "/pricing-seed.json").then((r) => {
          if (!r.ok) throw new Error("种子文件不可用(" + r.status + ")");
          return r.json();
        }).then((j) => doImport(j.pricing || j, "种子价目"))
          .catch((e) => msg.error(e.message))
          .then(() => { busy.seed = false; });
      };

      const pick = (files) => {
        const f = files && files[0] && (files[0].file || files[0]);
        if (!f) return;
        busy.imp = true;
        f.text().then((txt) => {
          const j = JSON.parse(txt);
          const list = Array.isArray(j) ? j : j.pricing;
          if (!Array.isArray(list)) throw new Error("格式应为 {pricing:[...]}");
          return doImport(list, "文件");
        }).catch((e) => msg.error("导入失败:" + e.message))
          .then(() => { busy.imp = false; file.value = null; });
      };

      const cols = [
        { title: "模型", key: "model_pattern", minWidth: 216, fixed: "left",
          render: (r) => stack(modelChip(r.model_pattern),
            r.notes || (r.group_id ? "限 " + gname(r.group_id) : "全局")) },
        { title: "范围", key: "group_id", width: 96,
          render: (r) => (r.group_id ? tag("info", gname(r.group_id))
            : tag("default", "全局")) },
        { title: "模式", key: "billing_mode", width: 88,
          render: (r) => tag(r.billing_mode === "free" ? "success"
            : r.billing_mode === "per_request" ? "warning" : "default",
            MODE[r.billing_mode] || r.billing_mode) },
        { title: "输入", key: "input_price", width: 104, align: "right",
          render: (r) => (r.billing_mode === "per_request"
            ? h("span", MONO_ATTR, "$" + fmt(r.per_request_price, 4) + " /次")
            : px(r.input_price)) },
        { title: "输出", key: "output_price", width: 100, align: "right",
          render: (r) => (r.billing_mode === "per_request" ? muted("—")
            : px(r.output_price)) },
        { title: "缓存读", key: "cache_read_price", width: 100, align: "right",
          render: (r) => (r.billing_mode === "per_request" ? muted("—")
            : px(r.cache_read_price)) },
        { title: "缓存写", key: "cache_write_price", width: 100, align: "right",
          render: (r) => (r.billing_mode === "per_request" ? muted("—")
            : px(r.cache_write_price)) },
        { title: "阶梯阈值", key: "long_threshold", width: 118, align: "right",
          render: (r) => (r.long_threshold
            ? h("span", { style: Object.assign({ color: C.ctx, fontWeight: 600 },
              MONO) }, "> " + nf(r.long_threshold)) : muted("不启用")) },
        { title: "长·输入", key: "long_input_price", width: 100, align: "right",
          render: (r) => (r.long_threshold ? px(r.long_input_price) : muted("—")) },
        { title: "长·输出", key: "long_output_price", width: 100, align: "right",
          render: (r) => (r.long_threshold ? px(r.long_output_price) : muted("—")) },
        { title: "长·缓存读", key: "long_cache_read_price", width: 108,
          align: "right",
          render: (r) => (r.long_threshold ? px(r.long_cache_read_price)
            : muted("—")) },
        { title: "长·缓存写", key: "long_cache_write_price", width: 108,
          align: "right",
          render: (r) => (r.long_threshold ? px(r.long_cache_write_price)
            : muted("—")) },
        { title: "操作", key: "act", width: 118, fixed: "right",
          render: (r) => h(naive.NSpace, { size: 6 }, () => [
            h(naive.NButton, { size: "tiny", secondary: true,
              onClick: () => edit(r) }, () => "编辑"),
            h(naive.NButton, { size: "tiny", secondary: true, type: "error",
              onClick: () => del(r) }, () => "删除"),
          ]) },
      ];
      return Object.assign({ rows, view, cat, cols, p, busy, filter, file, showEditor, openNew, gname,
        gopts, longCount, save, exportJson, seed, pick,
        MODE_OPTS, nf, fmt, MONO,
        reset: () => { Object.assign(p, NEW_PRICE()); msg.info("已清空表单"); } }, s);
    },
    template: `
<Load :loading="loading" :error="error" :on-retry="reload">
<AdminIntro title="模型定价" note="查看当前价格与覆盖范围，按模型配置新的计费规则。改价作用于后续请求，历史账单继续使用当时的快照。">
  <n-button type="primary" @click="openNew">新增定价</n-button>
</AdminIntro>
<div class="console-resource-strip">
  <div class="console-inline-stat"><span class="console-inline-stat-value">{{ nf(rows.length) }}</span><span class="console-inline-stat-label">自定义价格</span></div>
  <div class="console-inline-stat"><span class="console-inline-stat-value">{{ nf(longCount) }}</span><span class="console-inline-stat-label">长上下文阶梯</span></div>
  <div class="console-inline-stat"><span class="console-inline-stat-value">{{ nf(cat.entries || 0) }}</span><span class="console-inline-stat-label">兜底价目表模型</span></div>
</div>
<n-modal v-model:show="showEditor" preset="card" title="新增 / 更新定价" style="width:min(800px, calc(100vw - 32px))">
  <template #header-extra><n-text depth="3" style="font-size:11.5px">
    单价单位:美元 / 1M token</n-text></template>
  <n-text depth="3" style="font-size:12.5px">
    模型名支持 prefix* 通配,同「模型名 + 范围」再保存一次即覆盖原有定价。
    阶梯阈值填 0 表示不启用长上下文跳档;启用后长档 4 项留空的会逐项回落普通价,
    不会被当成 0。
  </n-text>
  <n-grid :cols="'1 560:3'" :x-gap="12" :y-gap="12" responsive="self"
    style="margin-top:14px">
    <n-gi :span="2"><n-form-item label="模型名" :show-feedback="false"
      label-placement="top"><n-input v-model:value="p.model_pattern"
      placeholder="kg-gpt-5.6-sol 或 kg-*"/></n-form-item></n-gi>
    <n-gi><n-form-item label="范围" :show-feedback="false" label-placement="top">
      <n-select v-model:value="p.group_id" :options="gopts"/>
    </n-form-item></n-gi>
    <n-gi><n-form-item label="计费模式" :show-feedback="false"
      label-placement="top"><n-select v-model:value="p.billing_mode"
      :options="MODE_OPTS"/></n-form-item></n-gi>
    <n-gi><n-form-item label="按次单价" :show-feedback="false"
      label-placement="top"><n-input-number v-model:value="p.per_request_price"
      :min="0" :step="0.001" style="width:100%"
      :disabled="p.billing_mode !== 'per_request'"/></n-form-item></n-gi>

    <n-gi><n-form-item label="输入 /1M" :show-feedback="false"
      label-placement="top"><n-input-number v-model:value="p.input_price"
      :min="0" :step="0.1" style="width:100%"
      :disabled="p.billing_mode !== 'token'"/></n-form-item></n-gi>
    <n-gi><n-form-item label="输出 /1M" :show-feedback="false"
      label-placement="top"><n-input-number v-model:value="p.output_price"
      :min="0" :step="0.1" style="width:100%"
      :disabled="p.billing_mode !== 'token'"/></n-form-item></n-gi>
    <n-gi><n-form-item label="缓存读 /1M" :show-feedback="false"
      label-placement="top"><n-input-number v-model:value="p.cache_read_price"
      :min="0" :step="0.01" style="width:100%"
      :disabled="p.billing_mode !== 'token'"/></n-form-item></n-gi>
    <n-gi><n-form-item label="缓存写 /1M" :show-feedback="false"
      label-placement="top"><n-input-number v-model:value="p.cache_write_price"
      :min="0" :step="0.01" style="width:100%"
      :disabled="p.billing_mode !== 'token'"/></n-form-item></n-gi>
    <n-gi><n-form-item label="阶梯阈值(0=不启用)" :show-feedback="false"
      label-placement="top"><n-input-number v-model:value="p.long_threshold"
      :min="0" :step="1000" style="width:100%"
      :disabled="p.billing_mode !== 'token'"/></n-form-item></n-gi>

    <n-gi><n-form-item label="长·输入 /1M" :show-feedback="false"
      label-placement="top"><n-input-number v-model:value="p.long_input_price"
      :min="0" :step="0.1" style="width:100%" clearable placeholder="留空回落"
      :disabled="!p.long_threshold"/></n-form-item></n-gi>
    <n-gi><n-form-item label="长·输出 /1M" :show-feedback="false"
      label-placement="top"><n-input-number v-model:value="p.long_output_price"
      :min="0" :step="0.1" style="width:100%" clearable placeholder="留空回落"
      :disabled="!p.long_threshold"/></n-form-item></n-gi>
    <n-gi><n-form-item label="长·缓存读 /1M" :show-feedback="false"
      label-placement="top">
      <n-input-number v-model:value="p.long_cache_read_price" :min="0"
      :step="0.01" style="width:100%" clearable placeholder="留空回落"
      :disabled="!p.long_threshold"/></n-form-item></n-gi>
    <n-gi><n-form-item label="长·缓存写 /1M" :show-feedback="false"
      label-placement="top">
      <n-input-number v-model:value="p.long_cache_write_price" :min="0"
      :step="0.01" style="width:100%" clearable placeholder="留空回落"
      :disabled="!p.long_threshold"/></n-form-item></n-gi>
    <n-gi :span="2"><n-form-item label="备注" :show-feedback="false"
      label-placement="top"><n-input v-model:value="p.notes"
      placeholder="可空,列表里会显示在模型名下方"/></n-form-item></n-gi>
  </n-grid>
  <div class="admin-impact" style="margin-top:16px">
    保存后，{{ gname(p.group_id) || '所有分组' }}中匹配「{{ p.model_pattern || '待填写模型名' }}」的后续请求会按定价优先级匹配此规则。
    <template v-if="p.billing_mode === 'free'">此规则按免费计费。</template>
    <template v-else-if="p.billing_mode === 'per_request'">按每次请求 \${{ fmt(p.per_request_price || 0, 4) }} 计价，再按分组策略结算。</template>
    <template v-else>按输入、输出及缓存用量分别计价，再按分组策略结算。</template>
    同模型名与范围的现有规则会被覆盖。
  </div>
  <template #footer><n-space justify="end" align="center">
    <n-button size="small" @click="showEditor = false">取消</n-button>
    <n-button size="small" secondary @click="reset">清空</n-button>
    <n-button type="primary" size="small" :loading="busy.save" @click="save">
      保存定价</n-button>
  </n-space></template>
</n-modal>

<n-card class="console-table-card" size="small" title="当前价格">
  <div class="console-toolbar">
    <n-input v-model:value="filter" clearable placeholder="筛选模型名" style="width:240px;max-width:100%"/>
    <n-space :size="8" align="center">
    <n-button size="small" secondary :loading="busy.seed" @click="seed">
      导入种子价目</n-button>
    <n-upload :show-file-list="false" accept=".json" :default-upload="false"
      @change="(d) => pick(d.fileList)">
      <n-button size="small" secondary :loading="busy.imp">导入 JSON</n-button>
    </n-upload>
    <n-button size="small" secondary :disabled="!rows.length"
      @click="exportJson">导出 JSON</n-button>
    </n-space>
  </div>
  <template #header-extra><n-text depth="3">{{ view.length }} / {{ rows.length }} 条</n-text></template>
  <n-data-table :columns="cols" :data="view" size="small" :bordered="false"
    :single-line="false" :scroll-x="1560" :max-height="520" virtual-scroll
    :row-key="(r) => r.id"/>
  <n-text depth="3" style="font-size:11.5px;display:block;margin-top:10px">
    解析顺序:分组专属精确名 → 分组专属 prefix* → 全局精确名 → 全局 prefix* →
    内置价目表 → 免费兜底。改价只影响之后的请求,历史账单读各自快照。
  </n-text>
</n-card>
</Load>`,
  };
  /* ---------------- tab 4 用户 ---------------- */

  const PAGE = 20;

  const Users = {
    components: { Load, AdminIntro },
    setup() {
      const rows = ref([]);
      const total = ref(0);
      const page = ref(1);
      const groups = ref([]);
      const q = ref("");
      const cur = ref(null);          // 当前操作的用户(抽屉)
      const userTask = ref("ledger");
      const led = ref([]);
      const ledLoading = ref(false);
      const pw = ref("");
      const adj = reactive({ amount: 10, notes: "" });
      const busy = reactive({ pw: false, adj: false });

      const s = useFetch(() => Promise.all([
        api("GET", "/admin/users" + qs({ q: q.value.trim(), limit: PAGE,
          offset: (page.value - 1) * PAGE })),
        api("GET", "/admin/groups"),
      ]).then((r) => {
        rows.value = r[0].users || [];
        total.value = r[0].total || 0;
        groups.value = r[1].groups || [];
      }));
      /* 检索在服务端做:原先只在当前页 20 人里本地筛,第 21 个用户永远搜不到。
         敲字停 300ms 再查,回第一页。 */
      let qTimer = null;
      watch(q, () => {
        clearTimeout(qTimer);
        qTimer = setTimeout(() => { page.value = 1; s.reload(); }, 300);
      });

      const gopts = computed(() => groups.value.map(
        (g) => ({ label: g.name, value: g.id })));
      const gname = (id) => {
        const g = groups.value.filter((x) => x.id === id)[0];
        return g ? g.name : (id ? "#" + id : "未分组");
      };
      const view = computed(() => rows.value);
      const admins = computed(() => rows.value.filter(
        (u) => u.role === "admin" && u.status === "active").length);

      const patch = (u, fields, okText) =>
        api("PATCH", "/admin/users/" + u.id, fields)
          .then(() => { msg.success(okText || "已更新"); return s.reload(); })
          /* 最后一个 admin 保护由后端把关,这里只负责把原因显示出来并回滚界面。 */
          .catch((e) => { msg.error(e.message); return s.reload(); });

      const openLedger = (u) => {
        cur.value = u;
        userTask.value = "ledger";
        pw.value = "";
        adj.amount = 10;
        adj.notes = "";
        led.value = [];
        ledLoading.value = true;
        api("GET", "/admin/ledger?user_id=" + u.id + "&limit=50")
          .then((r) => { led.value = r.entries || []; })
          .catch((e) => msg.error(e.message))
          .then(() => { ledLoading.value = false; });
      };

      const resetPw = () => {
        if (pw.value.length < 6) return msg.warning("新密码至少 6 位");
        busy.pw = true;
        api("POST", "/admin/users/" + cur.value.id + "/password",
          { new_password: pw.value })
          .then(() => { msg.success("已重置 " + cur.value.email + " 的密码");
            pw.value = ""; })
          .catch((e) => msg.error(e.message))
          .then(() => { busy.pw = false; });
      };
      /* 手机丢了的兜底。不能替自己关(后端也拒):自己的走个人资料页,要密码 + 验证码。 */
      const disableTotp = () => dlg.warning({
        title: "关闭 " + cur.value.email + " 的二次验证",
        content: "只在对方确实丢了 authenticator 时做。关闭后对方下次登录只要密码,"
          + "应尽快重新绑定。",
        positiveText: "关闭", negativeText: "取消",
        onPositiveClick: () => api("POST", "/admin/users/" + cur.value.id + "/2fa/disable")
          .then(() => { msg.success("已关闭"); cur.value.totp_enabled = false; return s.reload(); })
          .catch((e) => msg.error(e.message)),
      });

      const doAdjust = () => {
        const amt = Number(adj.amount);
        if (!amt) return msg.warning("调整额不能为 0");
        dlg.warning({
          title: (amt > 0 ? "增加" : "扣减") + "余额",
          content: "将给 " + cur.value.email + (amt > 0 ? " 增加 $" : " 扣减 $") +
            Math.abs(amt).toFixed(2) + "。这会写入余额流水且不可撤销," +
            "需要纠正只能再做一笔反向调整。",
          positiveText: "确认调额", negativeText: "取消",
          onPositiveClick: () => {
            busy.adj = true;
            return api("POST", "/admin/users/" + cur.value.id + "/balance",
              { amount: amt, notes: adj.notes || null })
              .then((r) => {
                msg.success("已调额,当前余额 $" + fmt(r.balance));
                adj.notes = "";
                openLedger(cur.value);
                return s.reload();
              }).catch((e) => msg.error(e.message))
              .then(() => { busy.adj = false; });
          },
        });
      };

      const cols = [
        { title: "用户", key: "email", minWidth: 210, fixed: "left",
          render: (r) => stack(h(naive.NSpace, { size: 5, align: "center" }, () => [
            h("span", { style: { fontWeight: 550 } }, r.email),
            r.role === "admin" ? tag("info", "管理员") : null,
            r.status === "active" ? null : tag("error", "已禁用"),
            r.email_verified_at ? null : tag("warning", "邮箱未验证"),
            /* 管理员没开二次验证要标出来:管理台管钱,这是最该有锁的账号 */
            r.totp_enabled ? tag("success", "2FA")
              : (r.role === "admin" ? tag("warning", "无 2FA") : null)]),
            "#" + r.id + " · 注册 " + relTime(r.created_at)) },
        { title: "分组", key: "group_id", width: 132,
          render: (r) => h(naive.NSelect, { size: "tiny", value: r.group_id,
            options: gopts.value, placeholder: "未分组", consistentMenuWidth: false,
            onUpdateValue: (v) => patch(r, { group_id: v },
              "已改为 " + gname(v)) }) },
        { title: "余额", key: "balance", width: 104, align: "right",
          render: (r) => h("span", { style: Object.assign({ fontWeight: 600,
            color: (r.balance || 0) > 0 ? C.cost : undefined }, MONO) },
            "$" + fmt(r.balance)) },
        { title: "累计消费", key: "total_spent", width: 108, align: "right",
          render: (r) => h("span", MONO_ATTR, "$" + fmt(r.total_spent)) },
        { title: "邀请码", key: "aff_code", width: 116,
          render: (r) => h(naive.NButton, { size: "tiny", text: true, style: MONO,
            onClick: () => copy(r.aff_code, "已复制邀请码") }, () => r.aff_code) },
        { title: "操作", key: "act", width: 244, fixed: "right",
          render: (r) => h(naive.NSpace, { size: 6 }, () => [
            h(naive.NButton, { size: "tiny", secondary: true,
              onClick: () => openLedger(r) }, () => "余额 · 密码"),
            h(naive.NButton, { size: "tiny", secondary: true,
              onClick: () => patch(r, { role: r.role === "admin" ? "user" : "admin" },
                r.role === "admin" ? "已降为普通用户" : "已升为管理员") },
              () => (r.role === "admin" ? "降权" : "设为管理员")),
            h(naive.NButton, { size: "tiny", secondary: true,
              type: r.status === "active" ? "warning" : "success",
              onClick: () => patch(r, { status: r.status === "active"
                ? "disabled" : "active" }) },
              () => (r.status === "active" ? "禁用" : "启用")),
          ]) },
      ];

      const lcols = [
        { title: "时间", key: "created_at", width: 148,
          render: (r) => h("span", MONO_ATTR, absTime(r.created_at)) },
        { title: "来源", key: "reason", width: 96,
          render: (r) => tag.apply(null, reasonOf(r.reason)) },
        { title: "金额", key: "amount", width: 96, align: "right",
          render: (r) => money(r.amount) },
        { title: "变动后", key: "balance_after", width: 96, align: "right",
          render: (r) => h("span", MONO_ATTR, "$" + fmt(r.balance_after)) },
      ];

      const jump = (n) => { page.value = n; s.reload(); };
      return Object.assign({ rows, view, total, page, cols, lcols, q, cur, led, userTask,
        ledLoading, pw, adj, busy, admins, resetPw, disableTotp, doAdjust, jump, gname,
        PAGE, fmt, nf, absTime, MONO,
        pages: computed(() => Math.max(1, Math.ceil(total.value / PAGE))) }, s);
    },
    template: `
<Load :loading="loading" :error="error" :on-retry="reload">
<AdminIntro title="用户" note="查找账号、核对分组与余额。打开用户后先查看流水，再按需要调整余额或处理账号安全。"/>
<n-card class="console-table-card" size="small" title="用户目录">
  <div class="console-toolbar">
    <n-input v-model:value="q" clearable
      placeholder="搜索邮箱、昵称、邀请码或用户 ID" style="width:360px;max-width:100%"/>
    <n-text depth="3">{{ q ? '匹配' : '共' }} {{ nf(total) }} 人 · 本页在职管理员 {{ admins }}</n-text>
  </div>
  <n-data-table :columns="cols" :data="view" size="small" :bordered="false"
    :single-line="false" :scroll-x="1060" :row-key="(r) => r.id"/>
  <n-space justify="space-between" align="center" style="margin-top:12px">
    <n-text depth="3" style="font-size:11.5px">
      系统禁止把最后一位在职管理员降权或禁用,后端会直接拒绝并给出原因。
    </n-text>
    <n-pagination v-if="pages > 1" :page="page" :page-count="pages"
      :page-slot="7" @update:page="jump"/>
  </n-space>
</n-card>

<n-drawer :show="!!cur" width="min(520px, 100vw)" placement="right"
  @update:show="(v) => { if (!v) cur = null; }">
  <n-drawer-content v-if="cur" :title="cur.email" closable>
    <n-space vertical :size="16">
      <n-descriptions :column="2" label-placement="top" size="small"
        :label-style="{opacity:.6,fontSize:'11px'}">
        <n-descriptions-item label="当前余额">
          <span :style="MONO">\${{ fmt(cur.balance) }}</span>
        </n-descriptions-item>
        <n-descriptions-item label="累计消费">
          <span :style="MONO">\${{ fmt(cur.total_spent) }}</span>
        </n-descriptions-item>
        <n-descriptions-item label="分组">{{ gname(cur.group_id) }}
        </n-descriptions-item>
        <n-descriptions-item label="邀请人">
          <span :style="MONO">{{ cur.inviter_id ? '#' + cur.inviter_id : '—' }}</span>
        </n-descriptions-item>
      </n-descriptions>

      <n-radio-group v-model:value="userTask" size="small" aria-label="用户操作">
        <n-radio-button value="ledger">余额流水</n-radio-button>
        <n-radio-button value="balance">调整余额</n-radio-button>
        <n-radio-button value="security">账号安全</n-radio-button>
      </n-radio-group>
      <n-card v-if="userTask === 'balance'" size="small" title="调整余额" :bordered="false" embedded>
        <n-space vertical :size="10">
          <n-input-number v-model:value="adj.amount" :step="10" style="width:100%">
            <template #prefix>$</template></n-input-number>
          <n-input v-model:value="adj.notes" placeholder="备注(可空,写进流水 meta)"/>
          <n-text depth="3" style="font-size:11px">
            正数为增加,负数为扣减。每次确认都会产生一笔新的余额流水。
            本次调整后余额为 \${{ fmt(Number(cur.balance) + Number(adj.amount || 0)) }}。
          </n-text>
        </n-space>
        <template #action><n-button size="small" type="primary"
          :loading="busy.adj" @click="doAdjust">确认调额</n-button></template>
      </n-card>

      <n-card v-if="userTask === 'security'" size="small" title="重置密码" :bordered="false" embedded>
        <n-input-group>
          <n-input v-model:value="pw" type="password" show-password-on="click"
            placeholder="新密码,至少 6 位"/>
          <n-button type="primary" ghost :loading="busy.pw" @click="resetPw">
            重置</n-button>
        </n-input-group>
        <n-text depth="3" style="font-size:11px;display:block;margin-top:8px">
          管理员重置无需旧密码,重置后请自行把新密码告知用户;对方所有已登录会话随之作废。
        </n-text>
        <n-button v-if="cur.totp_enabled" size="small" secondary type="warning"
          style="margin-top:10px" @click="disableTotp">关闭对方的二次验证(手机丢了用)</n-button>
      </n-card>

      <div v-if="userTask === 'ledger'">
        <n-space justify="space-between" align="center" style="margin-bottom:8px">
          <n-text style="font-size:13px;font-weight:550">余额流水</n-text>
          <n-text depth="3" style="font-size:11px">最近 50 条</n-text>
        </n-space>
        <n-spin :show="ledLoading">
          <n-data-table v-if="led.length" :columns="lcols" :data="led" size="small"
            :bordered="false" :single-line="false" :max-height="300"
            :row-key="(r) => r.id"/>
          <n-empty v-else-if="!ledLoading" description="暂无流水" size="small"
            style="padding:20px 0"/>
          <div v-else style="min-height:90px"></div>
        </n-spin>
      </div>
    </n-space>
  </n-drawer-content>
</n-drawer>
</Load>`,
  };
  /* ---------------- tab 5 兑换码与订单 ---------------- */

  const CODE_EXPIRY = [["永不过期", 0], ["7 天", 7 * 86400], ["30 天", 30 * 86400],
    ["90 天", 90 * 86400], ["1 年", 365 * 86400]];

  const Codes = {
    components: { Load, StatCard, AdminIntro },
    setup() {
      const showGenerator = ref(false);
      const codes = ref([]);
      const orders = ref([]);
      /* 空串而非 null 表示「全部」:null 会被 n-select 当成未选中而显示英文占位符。 */
      const filt = ref("");
      const gen = reactive({ count: 10, value: 10, ttl: 0, notes: "" });
      const busy = ref(false);
      const rq = reactive({});        // 每笔单自己的查单 loading
      const fresh = ref(null);        // 刚生成的一批,只展示这一次

      const s = useFetch(() => Promise.all([
        /* 只取余额码:邀请码同表不同 type(见「邀请码」页),混进来是一堆 $0.00 的行。 */
        api("GET", "/admin/codes?type=balance&limit=200" +
          (filt.value ? "&status=" + filt.value : "")),
        api("GET", "/admin/orders?limit=100"),
      ]).then((r) => {
        codes.value = r[0].codes || [];
        orders.value = r[1].orders || [];
      }));

      const stats = computed(() => {
        const all = codes.value;
        const unused = all.filter((c) => c.status === "unused");
        const used = all.filter((c) => c.status === "used");
        const sum = (l) => l.reduce((a, c) => a + (c.value || 0), 0);
        const paid = orders.value.filter((o) => o.status === "completed");
        return { unused: unused.length, unusedVal: sum(unused),
          used: used.length, usedVal: sum(used),
          orders: orders.value.length, paid: paid.length,
          paidVal: paid.reduce((a, o) => a + (o.amount || 0), 0) };
      });

      const doGen = () => {
        const n = Number(gen.count) || 0;
        const v = Number(gen.value);
        if (n < 1 || n > 1000) return msg.warning("数量需在 1~1000 之间");
        if (!v) return msg.warning("面额不能为 0");
        busy.value = true;
        api("POST", "/admin/codes", { count: n, value: v,
          expires_at: gen.ttl ? Math.floor(Date.now() / 1000) + gen.ttl : 0,
          notes: gen.notes || null })
          .then((r) => {
            showGenerator.value = false;
            fresh.value = r.codes || [];
            msg.success("已生成 " + fresh.value.length + " 个兑换码");
            return s.reload();
          }).catch((e) => msg.error(e.message))
          .then(() => { busy.value = false; });
      };

      const requery = (o) => {
        rq[o.out_trade_no] = true;
        api("POST", "/admin/orders/" + encodeURIComponent(o.out_trade_no) + "/requery")
          .then((r) => {
            if (r.credited) msg.success("已补到账 " + o.out_trade_no);
            else msg.info("上游状态:" + (r.upstream_status || "未知") + ",未到账");
            return s.reload();
          }).catch((e) => msg.error(e.message))
          .then(() => { rq[o.out_trade_no] = false; });
      };

      const disable = (c) => dlg.warning({
        title: "作废兑换码",
        content: "作废 " + c.code + " 后无法再兑换,且不可恢复。已被使用的码不受影响。",
        positiveText: "作废", negativeText: "取消",
        onPositiveClick: () => api("DELETE", "/admin/codes/" + c.code)
          .then(() => { msg.success("已作废"); return s.reload(); })
          .catch((e) => msg.error(e.message)),
      });

      const copyAll = () => copy((fresh.value || []).join("\n"),
        "已复制 " + (fresh.value || []).length + " 个兑换码");
      const exportFresh = () => {
        const url = URL.createObjectURL(new Blob(
          [(fresh.value || []).join("\r\n")], { type: "text/plain" }));
        const a = document.createElement("a");
        a.href = url;
        a.download = "bitapi-codes-" + Date.now() + ".txt";
        a.click();
        URL.revokeObjectURL(url);
      };

      const ccols = [
        { title: "兑换码", key: "code", width: 208, fixed: "left",
          render: (c) => h(naive.NButton, { size: "tiny", text: true, style: MONO,
            onClick: () => copy(c.code, "已复制") }, () => c.code) },
        { title: "面额", key: "value", width: 92, align: "right",
          render: (c) => h("span", { style: Object.assign({ fontWeight: 600,
            color: (c.value || 0) < 0 ? C.err : C.cost }, MONO) },
            (c.value < 0 ? "-$" : "$") + Math.abs(c.value || 0).toFixed(2)) },
        { title: "状态", key: "status", width: 92,
          render: (c) => {
            const expired = c.status === "unused" && c.expires_at &&
              c.expires_at < Math.floor(Date.now() / 1000);
            if (expired) return tag("warning", "已过期");
            /* 码被作废与用户被禁用共用 disabled,但这里的说法应当是「已作废」。 */
            if (c.status === "disabled") return tag("error", "已作废");
            return tag(st(c.status)[0], st(c.status)[1]); } },
        { title: "使用者", key: "used_by", width: 116,
          render: (c) => (c.used_by
            ? stack(h("span", MONO_ATTR, "#" + c.used_by),
              c.used_at ? absTime(c.used_at).slice(5) : null) : muted("—")) },
        { title: "有效期", key: "expires_at", width: 148,
          render: (c) => (c.expires_at ? h("span", MONO_ATTR,
            absTime(c.expires_at)) : muted("永不过期")) },
        { title: "备注", key: "notes", minWidth: 120,
          render: (c) => (c.notes ? c.notes : muted("—")) },
        { title: "创建", key: "created_at", width: 148,
          render: (c) => h("span", MONO_ATTR, absTime(c.created_at)) },
        { title: "操作", key: "act", width: 74, fixed: "right",
          render: (c) => (c.status === "unused"
            ? h(naive.NButton, { size: "tiny", secondary: true, type: "error",
              onClick: () => disable(c) }, () => "作废") : muted("—")) },
      ];

      const ocols = [
        { title: "订单号", key: "out_trade_no", width: 210, fixed: "left",
          render: (o) => h(naive.NButton, { size: "tiny", text: true, style: MONO,
            onClick: () => copy(o.out_trade_no, "已复制订单号") },
            () => o.out_trade_no) },
        { title: "用户", key: "user_id", width: 78,
          render: (o) => h("span", MONO_ATTR, "#" + o.user_id) },
        { title: "金额", key: "amount", width: 118, align: "right",
          render: (o) => stack(h("span", { style: Object.assign(
            { fontWeight: 600 }, MONO) }, "$" + fmt(o.amount, 2)),
            o.pay_amount_mao != null ? "应付 " + o.pay_amount_mao + " 毛" : o.pay_amount && o.pay_amount !== o.amount
              ? "实付 $" + fmt(o.pay_amount, 2) : null, "flex-end") },
        { title: "渠道", key: "provider", width: 96,
          render: (o) => tag("default", o.provider === "baipiao" ? "毛支付" : o.provider) },
        { title: "状态", key: "status", width: 96,
          render: (o) => tag(st(o.status)[0], st(o.status)[1]) },
        { title: "到账码", key: "recharge_code", width: 180,
          render: (o) => (o.recharge_code
            ? h("span", { style: Object.assign({ fontSize: "11.5px",
              opacity: 0.7 }, MONO) }, o.recharge_code) : muted("—")) },
        { title: "创建 / 完成", key: "created_at", width: 154,
          render: (o) => stack(h("span", MONO_ATTR, absTime(o.created_at)),
            o.completed_at ? "完成 " + absTime(o.completed_at).slice(5) : null) },
        /* 人工查单。自动对账每 600 秒一跳,这个按钮给「用户在线催单」和
           「自动路径也没救回来」两种情况。到账走订单自己的 recharge_code,
           所以流水 reason=recharge 带订单号 —— 手工调额那笔 reason=admin,
           事后跟任何订单都对不上。 */
        { title: "操作", key: "act", width: 92, fixed: "right",
          render: (o) => (o.status === "completed" ? muted("—")
            : h(naive.NButton, { size: "tiny", secondary: true,
              loading: !!rq[o.out_trade_no],
              onClick: () => requery(o) }, () => "查单")) },
      ];

      return Object.assign({ codes, orders, ccols, ocols, gen, busy, fresh, filt, showGenerator,
        stats, doGen, copyAll, exportFresh, nf, fmt, MONO,
        expiryOpts: CODE_EXPIRY.map((x) => ({ label: x[0], value: x[1] })),
        statusOpts: [{ label: "全部", value: "" },
          { label: "未使用", value: "unused" }, { label: "已使用", value: "used" },
          { label: "已作废", value: "disabled" }],
        onFilt: (v) => { filt.value = v; s.reload(); } }, s);
    },
    template: `
<Load :loading="loading" :error="error" :on-retry="reload">
<AdminIntro title="兑换码与订单" note="跟进兑换与充值到账。下方汇总来自当前筛选的最近 200 个兑换码，以及最近 100 笔订单。">
  <n-button type="primary" @click="showGenerator = true">生成兑换码</n-button>
</AdminIntro>
<div class="console-resource-strip">
  <div class="console-inline-stat"><span class="console-inline-stat-value">{{ nf(stats.unused) }}</span><span class="console-inline-stat-label">当前列表未使用 · 面额 \${{ fmt(stats.unusedVal, 2) }}</span></div>
  <div class="console-inline-stat"><span class="console-inline-stat-value">{{ nf(stats.used) }}</span><span class="console-inline-stat-label">当前列表已兑换 · \${{ fmt(stats.usedVal, 2) }}</span></div>
  <div class="console-inline-stat"><span class="console-inline-stat-value">{{ nf(stats.paid) }} / {{ nf(stats.orders) }}</span><span class="console-inline-stat-label">最近订单已完成 · 到账 \${{ fmt(stats.paidVal, 2) }}</span></div>
</div>

<n-modal v-model:show="showGenerator" preset="card" title="批量生成兑换码" style="width:min(680px, calc(100vw - 32px))">
  <n-grid :cols="'1 620:4'" :x-gap="12" :y-gap="12" responsive="self">
    <n-gi><n-form-item label="数量(≤1000)" :show-feedback="false"
      label-placement="top"><n-input-number v-model:value="gen.count" :min="1"
      :max="1000" style="width:100%"/></n-form-item></n-gi>
    <n-gi><n-form-item label="面额(美元)" :show-feedback="false"
      label-placement="top"><n-input-number v-model:value="gen.value" :step="5"
      style="width:100%"><template #prefix>$</template></n-input-number>
    </n-form-item></n-gi>
    <n-gi><n-form-item label="有效期" :show-feedback="false"
      label-placement="top"><n-select v-model:value="gen.ttl"
      :options="expiryOpts"/></n-form-item></n-gi>
    <n-gi><n-form-item label="备注" :show-feedback="false"
      label-placement="top"><n-input v-model:value="gen.notes"
      placeholder="可空,如 618 活动"/></n-form-item></n-gi>
  </n-grid>
  <n-text depth="3" style="font-size:11.5px;display:block;margin-top:12px">
    面额允许填负数,用于扣款纠错。兑换走的是「先占用后发放」的原子更新,
    同一个码并发只会成功一次。支付到账也复用这条路径(订单生成一个内部码再兑付),
    所以充值在流水里显示为「充值到账」。
  </n-text>
  <div class="admin-impact" style="margin-top:16px">本次生成 {{ gen.count || 0 }} 个码，合计面额 \${{ fmt((gen.count || 0) * (gen.value || 0), 2) }}。每个码只能兑换一次，生成后可复制或下载本批次。</div>
  <template #footer><n-space justify="end"><n-button @click="showGenerator = false">取消</n-button><n-button type="primary" :loading="busy"
    @click="doGen">生成兑换码</n-button></n-space></template>
</n-modal>

<n-card class="console-table-card" size="small" title="兑换码" style="margin-bottom:24px">
  <template #header-extra><n-space :size="10" align="center">
    <n-select :value="filt" :options="statusOpts" size="small"
      style="width:112px" @update:value="onFilt"/>
    <n-text depth="3" style="font-size:11.5px">{{ codes.length }} 条(最近 200)
    </n-text>
  </n-space></template>
  <n-data-table :columns="ccols" :data="codes" size="small" :bordered="false"
    :single-line="false" :scroll-x="1080" :max-height="440" virtual-scroll
    :row-key="(c) => c.code"/>
</n-card>

<n-card class="console-table-card" size="small" title="充值订单">
  <template #header-extra><n-text depth="3" style="font-size:11.5px">
    最近 100 笔</n-text></template>
  <n-data-table :columns="ocols" :data="orders" size="small" :bordered="false"
    :single-line="false" :scroll-x="1032" :max-height="400"
    :row-key="(o) => o.out_trade_no"/>
  <n-text depth="3" style="font-size:11.5px;display:block;margin-top:10px">
    状态流转:待支付 → 已支付 → 到账中 → 已完成;旁支为已过期 / 失败。
    停在「到账中」说明拿到了支付回调但兑付未完成,可查服务端日志。
    「查单」按钮向上游问一次这笔的真实状态,查到已付就补到账 —— 后台每 10 分钟
    也会自动做一遍。
  </n-text>
</n-card>

<n-modal :show="!!fresh" preset="card" title="新生成的兑换码"
  style="max-width:560px" :bordered="false"
  @update:show="(v) => { if (!v) fresh = null; }">
  <n-alert type="warning" :bordered="false" style="margin-bottom:12px">
    列表页出于安全只显示码本身不显示明文导出,这一批请现在复制留存。
  </n-alert>
  <n-input :value="(fresh || []).join('\\n')" type="textarea" readonly
    :rows="10" :style="MONO"/>
  <template #footer><n-space justify="end">
    <n-button size="small" @click="exportFresh">下载 txt</n-button>
    <n-button size="small" type="primary" @click="copyAll">全部复制</n-button>
  </n-space></template>
</n-modal>
</Load>`,
  };

  /* ---------------- tab 6 邀请码 ---------------- */

  /* 注册邀请码与兑换码同一张表,靠 type 分(invitation / balance),所以列表、作废、
     过期判定全是同一套。区别只有两条:它不带面额,而且只在注册时消耗一次 ——
     兑换口会明确拒掉它,否则谁都能把待发的码逐张兑成 0 元、把码烧光。 */
  const Invites = {
    components: { Load, StatCard, AdminIntro },
    setup() {
      const showGenerator = ref(false);
      const rows = ref([]);
      const need = ref(false);        // 邮箱注册是否强制邀请码
      const communityOnly = ref(false);
      const filt = ref("");
      const gen = reactive({ count: 10, ttl: 0, notes: "" });
      const busy = ref(false);
      const fresh = ref(null);        // 刚生成的一批,只展示这一次

      /* 顺带取一次站点设置:发码的人第一个要确认的就是「门槛开着没有」,
         开关本身留在「站点设置」页,这里只读回显,不做第二个入口。 */
      const s = useFetch(() => Promise.all([
        api("GET", "/admin/codes?type=invitation&limit=200" +
          (filt.value ? "&status=" + filt.value : "")),
        api("GET", "/admin/settings"),
      ]).then((r) => {
        rows.value = r[0].codes || [];
        need.value = !!r[1].require_invite;
        communityOnly.value = !!r[1].registration_policy?.community_only;
      }));

      const now = () => Math.floor(Date.now() / 1000);
      /* 过期不改 status(库里仍是 unused),所以「可用」要在前端排掉它,
         否则统计会把一批过期码算成还能发。 */
      const gone = (c) => c.status === "unused" && c.expires_at &&
        c.expires_at < now();
      const stats = computed(() => ({
        usable: rows.value.filter((c) => c.status === "unused" && !gone(c)).length,
        used: rows.value.filter((c) => c.status === "used").length,
      }));
      const regBase = computed(() => location.origin + BASE +
        "/portal#/register?ref=");

      const doGen = () => {
        const n = Number(gen.count) || 0;
        if (n < 1 || n > 1000) return msg.warning("数量需在 1~1000 之间");
        busy.value = true;
        api("POST", "/admin/codes", { count: n, type: "invitation",
          expires_at: gen.ttl ? now() + gen.ttl : 0, notes: gen.notes || null })
          .then((r) => {
            showGenerator.value = false;
            fresh.value = r.codes || [];
            msg.success("已生成 " + fresh.value.length + " 个邀请码");
            return s.reload();
          }).catch((e) => msg.error(e.message))
          .then(() => { busy.value = false; });
      };

      const disable = (c) => dlg.warning({
        title: "作废邀请码",
        content: "作废 " + c.code + " 后不能再用于注册,且不可恢复。" +
          "已经用它注册成功的账号不受影响。",
        positiveText: "作废", negativeText: "取消",
        onPositiveClick: () => api("DELETE", "/admin/codes/" + c.code)
          .then(() => { msg.success("已作废"); return s.reload(); })
          .catch((e) => msg.error(e.message)),
      });

      const copyAll = () => copy((fresh.value || []).join("\n"),
        "已复制 " + (fresh.value || []).length + " 个邀请码");
      const exportFresh = () => {
        const url = URL.createObjectURL(new Blob(
          [(fresh.value || []).join("\r\n")], { type: "text/plain" }));
        const a = document.createElement("a");
        a.href = url;
        a.download = "bitapi-invites-" + Date.now() + ".txt";
        a.click();
        URL.revokeObjectURL(url);
      };

      const cols = [
        { title: "邀请码", key: "code", width: 208, fixed: "left",
          render: (c) => h(naive.NButton, { size: "tiny", text: true, style: MONO,
            onClick: () => copy(c.code, "已复制") }, () => c.code) },
        { title: "状态", key: "status", width: 92,
          render: (c) => {
            if (gone(c)) return tag("warning", "已过期");
            if (c.status === "disabled") return tag("error", "已作废");
            /* 用掉的邀请码换的是一个账号,不是一笔钱,所以说「已注册」而不是
               兑换码那边的「已使用」—— 筛选下拉里也是这个词。 */
            if (c.status === "used") return tag("default", "已注册");
            return tag(st(c.status)[0], st(c.status)[1]); } },
        { title: "注册用户", key: "used_by", width: 122,
          render: (c) => (c.used_by
            ? stack(h("span", MONO_ATTR, "#" + c.used_by),
              c.used_at ? absTime(c.used_at).slice(5) : null) : muted("—")) },
        { title: "有效期", key: "expires_at", width: 148,
          render: (c) => (c.expires_at ? h("span", MONO_ATTR, absTime(c.expires_at))
            : muted("永不过期")) },
        { title: "备注", key: "notes", minWidth: 120,
          render: (c) => (c.notes ? c.notes : muted("—")) },
        { title: "创建", key: "created_at", width: 148,
          render: (c) => h("span", MONO_ATTR, absTime(c.created_at)) },
        { title: "操作", key: "act", width: 74, fixed: "right",
          render: (c) => (c.status === "unused"
            ? h(naive.NButton, { size: "tiny", secondary: true, type: "error",
              onClick: () => disable(c) }, () => "作废") : muted("—")) },
      ];

      return Object.assign({ rows, cols, gen, busy, fresh, filt, need, communityOnly, stats, showGenerator,
        regBase, doGen, copyAll, exportFresh, nf, MONO,
        expiryOpts: CODE_EXPIRY.map((x) => ({ label: x[0], value: x[1] })),
        statusOpts: [{ label: "全部", value: "" },
          { label: "未使用", value: "unused" },
          { label: "已注册", value: "used" },
          { label: "已作废", value: "disabled" }],
        onFilt: (v) => { filt.value = v; s.reload(); } }, s);
    },
    template: `
<Load :loading="loading" :error="error" :on-retry="reload">
<AdminIntro title="邀请码" :note="communityOnly ? '社区等级注册已启用，历史邀请码不再作为注册凭证。' : '为新用户准备一次性注册凭证。数量统计基于最近 200 条记录。'">
  <n-button v-if="!communityOnly" type="primary" @click="showGenerator = true">生成邀请码</n-button>
</AdminIntro>
<div class="console-resource-strip">
  <div class="console-inline-stat"><span class="console-inline-stat-value">{{ nf(stats.usable) }}</span><span class="console-inline-stat-label">当前列表可用 · 未使用且未过期</span></div>
  <div class="console-inline-stat"><span class="console-inline-stat-value">{{ nf(stats.used) }}</span><span class="console-inline-stat-label">当前列表已用于注册</span></div>
  <div class="console-inline-stat"><span class="console-inline-stat-value">{{ communityOnly ? '社区 Lv3+' : need ? '需要邀请码' : '邮箱注册' }}</span><span class="console-inline-stat-label">当前注册方式</span></div>
</div>

<n-modal v-model:show="showGenerator" preset="card" title="批量生成邀请码" style="width:min(620px, calc(100vw - 32px))">
  <n-grid :cols="'1 620:3'" :x-gap="12" :y-gap="12" responsive="self">
    <n-gi><n-form-item label="数量(≤1000)" :show-feedback="false"
      label-placement="top"><n-input-number v-model:value="gen.count" :min="1"
      :max="1000" style="width:100%"/></n-form-item></n-gi>
    <n-gi><n-form-item label="有效期" :show-feedback="false"
      label-placement="top"><n-select v-model:value="gen.ttl"
      :options="expiryOpts"/></n-form-item></n-gi>
    <n-gi><n-form-item label="备注" :show-feedback="false"
      label-placement="top"><n-input v-model:value="gen.notes"
      placeholder="可空,如 内测第一批"/></n-form-item></n-gi>
  </n-grid>
  <n-text depth="3" style="font-size:11.5px;display:block;margin-top:12px">
    一个码只能注册一个账号,用掉即失效;它不带面额,也不绑邀请人 —— 要让新人算在某个
    老用户名下拿返佣,得让他填那个人的专属邀请码(「邀请返佣」页里那个,不限次数)。
    码可以直接给人填,也可以拼成注册链接:<span :style="MONO">{{ regBase }}码</span>
  </n-text>
  <div class="admin-impact" style="margin-top:16px">本次生成 {{ gen.count || 0 }} 个一次性注册凭证。邀请码不带余额，不建立返佣关系。</div>
  <template #footer><n-space justify="end"><n-button @click="showGenerator = false">取消</n-button><n-button type="primary" :loading="busy"
    @click="doGen">生成邀请码</n-button></n-space></template>
</n-modal>

<n-card class="console-table-card" size="small" title="注册凭证">
  <template #header-extra><n-space :size="10" align="center">
    <n-select :value="filt" :options="statusOpts" size="small"
      style="width:112px" @update:value="onFilt"/>
    <n-text depth="3" style="font-size:11.5px">{{ rows.length }} 条(最近 200)
    </n-text>
  </n-space></template>
  <n-data-table :columns="cols" :data="rows" size="small" :bordered="false"
    :single-line="false" :scroll-x="960" :max-height="440" virtual-scroll
    :row-key="(c) => c.code"/>
  <n-text depth="3" style="font-size:11.5px;display:block;margin-top:10px">
    过期的码库里仍记作未使用,只是注册时会被拒 —— 上面的「可用」已经把它们排掉了。
    作废只能作废还没被用掉的码。
  </n-text>
</n-card>

<n-modal :show="!!fresh" preset="card" title="新生成的邀请码"
  style="max-width:560px" :bordered="false"
  @update:show="(v) => { if (!v) fresh = null; }">
  <n-alert type="warning" :bordered="false" style="margin-bottom:12px">
    这一批请现在复制留存;之后列表里也能逐个复制,但没有批量导出。
  </n-alert>
  <n-input :value="(fresh || []).join('\\n')" type="textarea" readonly
    :rows="10" :style="MONO"/>
  <template #footer><n-space justify="end">
    <n-button size="small" @click="exportFresh">下载 txt</n-button>
    <n-button size="small" type="primary" @click="copyAll">全部复制</n-button>
  </n-space></template>
</n-modal>
</Load>`,
  };

  /* ---------------- tab 7 公告 ---------------- */

  const ANN_LEVEL_OPTS = ["info", "success", "warning", "error"].map(
    (v) => ({ label: P.ANN_LEVEL[v] + "(" + v + ")", value: v }));
  const ANN_MODE_OPTS = ["silent", "popup"].map(
    (v) => ({ label: P.ANN_MODE[v], value: v }));
  const NEW_ANN = () => ({ title: "", body: "", level: "info",
    pinned: false, active: true, notify_mode: "silent",
    starts_at: null, ends_at: null });
  /* 展示窗口用时间戳存(秒),Naive 的 DatePicker 给毫秒,两侧各转一次。
     不填 = 0 = 立即生效 / 永久,与后端口径一致。 */
  const toMs = (s) => (s ? s * 1000 : null);
  const toSec = (ms) => (ms ? Math.floor(ms / 1000) : 0);
  /* 已发布但不在窗口内时给个原因;在窗口内返回空串,由调用方判断是否加标签。 */
  const windowText = (r) => {
    const now = Math.floor(Date.now() / 1000);
    if (r.starts_at && now < r.starts_at) return "未开始";
    if (r.ends_at && now > r.ends_at) return "已过期";
    return "";
  };

  const Announcements = {
    components: { Load, AdminIntro },
    setup() {
      const rows = ref([]);
      const show = ref(false);
      const busy = ref(false);
      const editing = ref(null);        // null=新建,否则是被编辑那条的 id
      const f = reactive(NEW_ANN());
      const s = useFetch(() => api("GET", "/admin/announcements")
        .then((r) => { rows.value = r.announcements || []; }));

      /* 顶栏铃铛拉的是 /announcements(只给当前该展示的),这里改完要让它重取,
         否则管理员自己看不到刚发的公告。 */
      const notify = () => {
        window.dispatchEvent(new Event("bitapi:announcements-changed"));
        return s.reload();
      };

      const openNew = () => {
        editing.value = null;
        Object.assign(f, NEW_ANN());
        show.value = true;
      };
      const openEdit = (r) => {
        editing.value = r.id;
        Object.assign(f, { title: r.title, body: r.body, level: r.level,
          pinned: !!r.pinned, active: !!r.active,
          notify_mode: r.notify_mode || "silent",
          starts_at: toMs(r.starts_at), ends_at: toMs(r.ends_at) });
        show.value = true;
      };

      const submit = () => {
        if (!f.title.trim()) return msg.warning("请填写标题");
        if (!f.body.trim()) return msg.warning("请填写正文");
        if (f.starts_at && f.ends_at && f.ends_at <= f.starts_at)
          return msg.warning("结束时间要晚于开始时间");
        busy.value = true;
        const body = { title: f.title.trim(), body: f.body.trim(),
          level: f.level, pinned: !!f.pinned, active: !!f.active,
          notify_mode: f.notify_mode,
          starts_at: toSec(f.starts_at), ends_at: toSec(f.ends_at) };
        const p = editing.value
          ? api("PATCH", "/admin/announcements/" + editing.value, body)
          : api("POST", "/admin/announcements", body);
        p.then(() => {
          msg.success(editing.value ? "公告已更新" : "公告已发布");
          show.value = false;
          return notify();
        }).catch((e) => msg.error(e.message))
          .then(() => { busy.value = false; });
      };

      const patch = (r, fields, okText) =>
        api("PATCH", "/admin/announcements/" + r.id, fields)
          .then(() => { msg.success(okText || "已更新"); return notify(); })
          .catch((e) => { msg.error(e.message); return s.reload(); });

      const del = (r) => dlg.warning({
        title: "删除公告",
        content: "将删除「" + r.title + "」及它的已读记录。用户端立即不再显示," +
          "不可撤销。",
        positiveText: "删除", negativeText: "取消",
        onPositiveClick: () => api("DELETE", "/admin/announcements/" + r.id)
          .then(() => { msg.success("已删除"); return notify(); })
          .catch((e) => msg.error(e.message)),
      });

      const cols = [
        { title: "公告", key: "title", minWidth: 260, fixed: "left",
          render: (r) => stack(h(naive.NSpace, { size: 5, align: "center" },
            () => [tag(r.level, P.ANN_LEVEL[r.level] || r.level),
              h("span", { style: { fontWeight: 600 } }, r.title),
              r.pinned ? tag("default", "置顶") : null,
              r.notify_mode === "popup" ? tag("info", "弹窗") : null,
              r.active ? null : tag("error", "草稿"),
              /* 已发布但落在窗口外的,状态跟草稿不是一回事,要分开说。 */
              r.active && windowText(r) ? tag("warning", windowText(r)) : null]),
            /* 正文在列表里只给一行预览,全文在编辑弹窗里看。 */
            String(r.body || "").replace(/\s+/g, " ").slice(0, 68) +
              (String(r.body || "").length > 68 ? "…" : "")) },
        { title: "展示窗口", key: "starts_at", width: 148,
          render: (r) => (!r.starts_at && !r.ends_at ? muted("立即 · 永久")
            : stack(h("span", MONO_ATTR,
                r.starts_at ? absTime(r.starts_at).slice(0, 16) : "立即"),
              r.ends_at ? "至 " + absTime(r.ends_at).slice(0, 16) : "永久")) },
        { title: "已读", key: "read_count", width: 88, align: "right",
          render: (r) => h("span", MONO_ATTR,
            (r.read_count || 0) + " / " + (r.user_count || 0)) },
        /* 发布人并进发布时间的副行:单开一列要 168px,把「已读」挤到横向滚动里去了。 */
        { title: "发布", key: "created_at", width: 152,
          render: (r) => stack(h("span", MONO_ATTR, absTime(r.created_at)),
            (r.by || "—") + (r.updated_at && r.updated_at !== r.created_at
              ? " · 改于 " + relTime(r.updated_at) : "")) },
        { title: "操作", key: "act", width: 236, fixed: "right",
          render: (r) => h(naive.NSpace, { size: 6 }, () => [
            h(naive.NButton, { size: "tiny", secondary: true,
              onClick: () => openEdit(r) }, () => "编辑"),
            h(naive.NButton, { size: "tiny", secondary: true,
              onClick: () => patch(r, { pinned: !r.pinned },
                r.pinned ? "已取消置顶" : "已置顶") },
              () => (r.pinned ? "取消置顶" : "置顶")),
            h(naive.NButton, { size: "tiny", secondary: true,
              type: r.active ? "warning" : "success",
              onClick: () => patch(r, { active: !r.active },
                r.active ? "已下架" : "已发布") },
              () => (r.active ? "下架" : "发布")),
            h(naive.NButton, { size: "tiny", secondary: true, type: "error",
              onClick: () => del(r) }, () => "删除"),
          ]) },
      ];

      const live = computed(() => rows.value.filter(
        (r) => r.active && !windowText(r)).length);
      return Object.assign({ rows, cols, show, busy, f, editing, openNew,
        submit, live, ANN_LEVEL_OPTS, ANN_MODE_OPTS, nf }, s);
    },
    template: `
<Load :loading="loading" :error="error" :on-retry="reload">
<AdminIntro title="公告" note="向所有登录用户传达更新。控制展示时间与提醒方式，并查看每条公告的阅读情况。">
  <n-button type="primary" @click="openNew">发布公告</n-button>
</AdminIntro>
<div class="console-resource-strip">
  <div class="console-inline-stat"><span class="console-inline-stat-value">{{ nf(live) }}</span><span class="console-inline-stat-label">当前展示中</span></div>
  <div class="console-inline-stat"><span class="console-inline-stat-value">{{ nf(rows.length) }} / 50</span><span class="console-inline-stat-label">已保存公告 / 上限</span></div>
</div>
<n-card class="console-table-card" size="small" title="公告列表">
  <n-data-table :columns="cols" :data="rows" size="small" :bordered="false"
    :single-line="false" :scroll-x="1000" :row-key="(r) => r.id"/>
  <n-text depth="3" style="font-size:11.5px;display:block;margin-top:10px">
    公告出现在所有登录用户的顶栏铃铛里,置顶的排最前。已读状态按用户存库(换设备也
    保持),改动正文会让已读过的人重新看到红点。「已读」列是读过的人数 / 总用户数。
    上限 50 条。
  </n-text>
</n-card>

<n-modal :show="show" preset="card" style="max-width:600px" :bordered="false"
  :title="editing ? '编辑公告' : '发布公告'"
  @update:show="(v) => show = v">
  <n-space vertical :size="14">
    <n-form-item label="标题" :show-feedback="false" label-placement="top">
      <n-input v-model:value="f.title" placeholder="一句话说清这条公告是什么"
        maxlength="120" show-count/>
    </n-form-item>
    <n-form-item label="正文" :show-feedback="false" label-placement="top">
      <n-input v-model:value="f.body" type="textarea" :rows="6"
        placeholder="支持换行,按原样显示,不解析 Markdown"
        maxlength="4000" show-count/>
    </n-form-item>
    <n-grid :cols="2" :x-gap="12" :y-gap="12">
      <n-gi><n-form-item label="级别" :show-feedback="false"
        label-placement="top">
        <n-select v-model:value="f.level" :options="ANN_LEVEL_OPTS"/>
      </n-form-item></n-gi>
      <n-gi><n-form-item label="提醒方式" :show-feedback="false"
        label-placement="top">
        <n-select v-model:value="f.notify_mode" :options="ANN_MODE_OPTS"/>
      </n-form-item></n-gi>
      <n-gi><n-form-item label="开始展示(空=立即)" :show-feedback="false"
        label-placement="top">
        <n-date-picker v-model:value="f.starts_at" type="datetime" clearable
          style="width:100%"/>
      </n-form-item></n-gi>
      <n-gi><n-form-item label="结束展示(空=永久)" :show-feedback="false"
        label-placement="top">
        <n-date-picker v-model:value="f.ends_at" type="datetime" clearable
          style="width:100%"/>
      </n-form-item></n-gi>
    </n-grid>
    <n-space :size="24">
      <n-space :size="8" align="center">
        <span style="font-size:12.5px">置顶</span>
        <n-switch v-model:value="f.pinned" size="small"/>
      </n-space>
      <n-space :size="8" align="center">
        <span style="font-size:12.5px">立即发布</span>
        <n-switch v-model:value="f.active" size="small"/>
      </n-space>
    </n-space>
    <n-text depth="3" style="font-size:11.5px">
      「登录弹窗」会在用户下次进控制台时弹一次,关掉即算已读、不再弹;「仅铃铛」只进
      铃铛列表。关掉「立即发布」存成草稿,用户端看不到,之后在列表里点「发布」再放出去。
    </n-text>
  </n-space>
  <template #footer><n-space justify="end">
    <n-button size="small" @click="show = false">取消</n-button>
    <n-button size="small" type="primary" :loading="busy" @click="submit">
      {{ editing ? '保存' : '发布' }}</n-button>
  </n-space></template>
</n-modal>
</Load>`,
  };

  /* ---------------- 管理台外壳 ---------------- */

  /* ---------------- tab 0 看板 ---------------- */

  const Stats = {
    components: { Load, StatCard, BarChart, Donut, Pills, AdminIntro, Ic: P.Ic, ModelCachePanel: P.ModelCachePanel },
    setup() {
      const d = ref({});
      const days = ref(14);
      const poolChannels = ref([]);
      const poolError = ref("");
      const updatedAt = ref(null);
      const s = useFetch(() => Promise.all([
        api("GET", "/admin/stats?days=" + days.value).then((r) => { d.value = r; updatedAt.value = Date.now() / 1000; }),
        api("GET", "/admin/channels").then((r) => {
          poolChannels.value = r.channels || []; poolError.value = "";
        }).catch((e) => { poolError.value = e.message; }),
      ]));
      watch(days, () => s.reload());
      const w = (k) => (d.value.windows || {})[k] || {};
      const u = (k) => w(k).usage || {};
      const users = computed(() => d.value.users || {});
      const bal = computed(() => d.value.balances || {});
      const pool = computed(() => poolChannels.value.reduce((out, channel) => {
        out.active += Number((channel.pool || {}).active || 0);
        out.total += Number((channel.pool || {}).total || 0);
        out.online += channel.disabled ? 0 : 1;
        return out;
      }, { active: 0, total: 0, online: 0 }));

      /* Token 与缓存率显示全站累计；其余卡片沿用今日与近 7 天的窗口。 */
      const cards = computed(() => {
        const t = u("today"), wk = u("week");
        const totalUsage = d.value.total_usage || {};
        const totalTokens = totalUsage.tokens || 0;
        const cacheTokens = totalUsage.cache_tokens;
        const totalInput = cacheTokens == null || totalUsage.input_tokens == null ? null
          : Number(totalUsage.input_tokens) + Number(cacheTokens);
        const cacheRate = totalInput > 0 ? fmt(Number(cacheTokens) / totalInput * 100, 1) + "%" : "—";
        const tokenValue = kf(totalTokens);
        const failRate = (x) => (x.requests ? Math.round((x.failed || 0) / x.requests * 1000) / 10 : 0);
        return [
          { label: "今日请求", value: nf(t.requests || 0), value2: nf(wk.requests || 0), animate: true,
            sub: "副值为近 7 天", color: "#3f6b8a", icon: "doc" },
          { label: "全站总 Token 数", value: tokenValue, animate: true,
            sub: nf(totalTokens) + " Tokens · 输入 + 输出 + 缓存", color: "#6b5b95",
            icon: "bolt", tint: true },
          { label: "缓存率", value: cacheRate,
            sub: totalInput > 0 ? "全站累计 · 缓存 " + kf(cacheTokens) + " / 总输入 " + kf(totalInput)
              : "全站累计 · 暂无输入 Token",
            hint: "缓存 Token ÷（输入 Token + 缓存 Token），包含缓存读写，不计输出 Token。",
            color: "#4a6fa5", icon: "database" },
          { label: "今日充值", value: "$" + fmt(w("today").recharge || 0),
            value2: ((w("today").orders || {}).count || 0) + " 笔",
            sub: "7 天 $" + fmt(w("week").recharge || 0) + " · 实收 ¥" +
              fmt((w("week").orders || {}).cny || 0) + " / " +
              nf((w("week").orders || {}).mao || 0) + " 毛", color: "#15803d",
            icon: "wallet", tone: "success" },
          { label: "余额负债", value: "$" + fmt(bal.value.owed || 0),
            sub: "所有用户余额之和 · 透支 $" + fmt(Math.abs(bal.value.overdrawn || 0)),
            color: "#a8613c", icon: "coins" },
          { label: "活跃用户", value: nf(users.value.active_today || 0), animate: true,
            value2: nf(users.value.active_week || 0),
            sub: "今日 / 7 天有调用的人", color: "#4a6fa5", icon: "user" },
          { label: "新增用户", value: nf(users.value.new_today || 0), animate: true,
            value2: nf(users.value.new_month || 0),
            sub: "今日 / 30 天 · 总计 " + nf(users.value.total || 0),
            color: "#4d7fa0", icon: "gift" },
          { label: "今日失败率", value: failRate(t) + "%",
            sub: (t.failed || 0) + " 次未正常结束 · 7 天 " + failRate(wk) + "%",
            color: (t.failed || 0) ? "#b91c1c" : "#15803d", icon: "alert",
            tone: (t.failed || 0) ? "error" : "success" },
          { label: "今日平均响应", value: fmt((t.avg_ms || 0) / 1000, 2) + "s",
            sub: "7 天 " + fmt((wk.avg_ms || 0) / 1000, 2) + "s",
            tone: t.requests ? P.elapsedLv((t.avg_ms || 0) / 1000) : null,
            color: "#6b5b95", icon: "clock" },
        ];
      });

      const metric = ref("requests");
      const METRICS = [{ label: "请求", value: "requests" },
        { label: "Token", value: "tokens" }, { label: "实扣", value: "cost" },
        { label: "活跃用户", value: "users" }];
      const METRIC_COLOR = { requests: "var(--console-series-requests)", tokens: "var(--console-series-tokens)",
        cost: "var(--console-series-cost)", users: "var(--console-series-requests)" };
      const bars = computed(() => (d.value.series || []).map((x) => {
        const v = x[metric.value] || 0;
        return { label: x.label, value: v,
          text: metric.value === "cost" ? "$" + fmt(v)
            : metric.value === "tokens" ? nf(v) + " tok"
            : metric.value === "users" ? nf(v) + " 人" : nf(v) + " 次" };
      }));
      const barSum = computed(() => bars.value.reduce((a, x) => a + x.value, 0));

      const PALETTE = ["#5b8a6b", "#7d6da6", "#4d7fa0", "#97882f", "#a8613c",
        "#5b80b5", "#bf4038", "#94a3b8"];
      const pie = computed(() => (d.value.by_channel || []).map((r, i) => ({
        label: r.key || "—", value: r.actual_cost || 0,
        text: usd(r.actual_cost || 0), color: PALETTE[i % PALETTE.length],
        requests: r.requests, tokens: r.tokens })));
      const pieTotal = computed(() => pie.value.reduce((a, x) => a + x.value, 0));

      /* 30 天钱的去向。每一行都能在流水里按 reason 对回去 —— 看板不造新口径。 */
      const moneyRows = computed(() => {
        const m = w("month");
        return [
          ["充值到账", m.recharge || 0, "recharge", "已完成订单 " + ((m.orders || {}).count || 0) + " 笔 · 实收 ¥" + fmt((m.orders || {}).cny || 0) + " / " + nf((m.orders || {}).mao || 0) + " 毛"],
          ["兑换码入账", m.redeem || 0, "redeem", "含自售与赠送的码"],
          ["套餐售出", m.plans || 0, "plan", "用户用余额买时长卡"],
          ["消费扣款", m.consumed || 0, "usage", "余额策略下真实扣掉的"],
          ["营销赠出", m.giveaway || 0, "promo", "签到 + 注册赠额 + 返佣 + 手工加额"],
        ];
      });

      const aggCols = (label) => [
        { title: label, key: "key", minWidth: 200, render: (r) =>
            label === "模型" ? modelChip(r.key || "—") : h("span", { style: MONO }, r.key || "—") },
        { title: "请求", key: "requests", align: "right", width: 84,
          render: (r) => h("span", { style: MONO }, nf(r.requests)) },
        { title: "Tokens", key: "tokens", align: "right", width: 110,
          render: (r) => h("span", { style: MONO }, kf(r.tokens)) },
        { title: "实扣", key: "actual_cost", align: "right", width: 100,
          render: (r) => h("span", { style: Object.assign({ color: C.cost,
            fontWeight: 600 }, MONO) }, "$" + fmt(r.actual_cost)) },
        { title: "原价", key: "cost", align: "right", width: 96,
          render: (r) => h("span", { style: MONO }, "$" + fmt(r.cost)) },
      ];
      const userCols = [
        { title: "用户", key: "email", minWidth: 220, render: (r) => stack(
            h("span", { style: { fontWeight: 550 } }, r.email || ("#" + r.key)),
            "#" + r.key + (r.display_name ? " · " + r.display_name : "") +
              (r.status && r.status !== "active" ? " · 已禁用" : "")) },
      ].concat(aggCols("").slice(1));

      return Object.assign({ d, days, cards, metric, METRICS, METRIC_COLOR, bars, poolChannels, poolError, pool, updatedAt,
        barSum, pie, pieTotal, moneyRows, users, aggCols, userCols, nf, kf, fmt,
        MONO, absTime, reasonOf, tag }, s);
    },
    template: `
<Load :loading="loading" :error="error" :on-retry="reload">
<AdminIntro title="运营概览" note="从请求、资金与渠道资源出发，查看站点当前的业务量。各项数据标明自己的统计窗口。">
  <n-button secondary :loading="loading" @click="reload">刷新数据</n-button>
  <a class="console-text-link" href="#/admin?tab=logs">查看全站日志</a>
</AdminIntro>
<section class="console-panel admin-overview-focus">
  <div class="admin-overview-primary">
    <div class="console-stat-label"><Ic name="stream" :size="18"/>今日请求</div>
    <div class="admin-focus-value"><MetricValue :value="cards[0].value" :animate="cards[0].animate"/></div>
    <p class="console-section-note">近 7 天 {{ cards[0].value2 }} 次请求</p>
    <n-text depth="3" v-if="updatedAt">更新于 {{ absTime(updatedAt) }}</n-text>
  </div>
  <div class="admin-overview-metrics">
    <div class="console-inline-stat" v-for="c in cards.slice(1, 5)" :key="c.label" :class="c.tone && 'console-tone-' + c.tone" :title="c.hint">
      <span class="console-stat-label"><Ic :name="c.icon" :size="16"/>{{ c.label }}</span>
      <span class="console-inline-stat-value"><MetricValue :value="c.value" :animate="c.animate"/></span>
      <span class="console-section-note">{{ c.value2 ? c.value2 + ' · ' : '' }}{{ c.sub }}</span>
    </div>
  </div>
</section>

<div class="admin-kpi-grid">
  <div class="console-inline-stat" v-for="c in cards.slice(5)" :key="c.label" :class="c.tone && 'console-tone-' + c.tone">
    <span class="console-stat-label"><Ic :name="c.icon" :size="16"/>{{ c.label }}</span>
    <span class="console-inline-stat-value"><MetricValue :value="c.value" :animate="c.animate"/><small v-if="c.value2"> / {{ c.value2 }}</small></span>
    <span class="console-section-note">{{ c.sub }}</span>
  </div>
</div>

<div class="console-workspace-grid">
    <n-card size="small" :title="'近 ' + days + ' 天趋势'">
      <div class="console-toolbar">
        <n-space align="center" :size="10" wrap>
          <n-radio-group v-model:value="days" size="small">
            <n-radio-button :value="7">7 天</n-radio-button>
            <n-radio-button :value="14">14 天</n-radio-button>
            <n-radio-button :value="30">30 天</n-radio-button>
            <n-radio-button :value="90">90 天</n-radio-button>
          </n-radio-group>
          <n-radio-group v-model:value="metric" size="small">
            <n-radio-button v-for="m in METRICS" :key="m.value" :value="m.value">
              {{ m.label }}</n-radio-button>
          </n-radio-group>
        </n-space>
      </div>
      <BarChart :items="bars" :color="METRIC_COLOR[metric]" :height="210"/>
      <n-text depth="3" style="font-size:11.5px;display:block;margin-top:10px">
        合计 <n-text :style="MONO">{{ metric === 'cost' ? '$' + fmt(barSum) : nf(barSum) }}</n-text>
        · 按本地日历日分桶。
      </n-text>
    </n-card>
    <n-card size="small" title="渠道资源池">
      <template #header-extra><a class="console-text-link" href="#/admin?tab=channels">管理渠道</a></template>
      <n-alert v-if="poolError" type="warning" :show-icon="false">渠道资源读取失败：{{ poolError }}。可刷新重试。</n-alert>
      <template v-else>
        <div class="console-resource-strip">
          <div class="console-inline-stat"><span class="console-inline-stat-value">{{ nf(pool.active) }} / {{ nf(pool.total) }}</span><span class="console-inline-stat-label">号池可用 / 总账号</span></div>
          <div class="console-inline-stat"><span class="console-inline-stat-value">{{ pool.online }} / {{ poolChannels.length }}</span><span class="console-inline-stat-label">已上线 / 全部渠道</span></div>
        </div>
        <div class="admin-pool-list">
          <a v-for="channel in poolChannels.slice(0, 6)" :key="channel.name" class="admin-pool-row" href="#/admin?tab=channels">
            <div><strong>{{ channel.name }}</strong><small>{{ channel.disabled ? '已下线' : '已上线' }} · {{ (channel.models || []).length }} 个模型</small></div>
            <span :style="MONO">{{ nf((channel.pool || {}).active || 0) }} / {{ nf((channel.pool || {}).total || 0) }}</span>
          </a>
        </div>
        <n-empty v-if="!poolChannels.length" description="尚未配置渠道" size="small"/>
        <p class="console-section-note">账号可用数包含已下线渠道的号池；渠道是否上线与账号是否可用分别统计。</p>
      </template>
    </n-card>
</div>

<div class="console-workspace-grid" style="margin-top:24px">
    <n-card size="small" title="资金流向 · 近 30 天">
      <n-space vertical :size="16">
        <div v-for="r in moneyRows" :key="r[2]" class="admin-money-row">
          <div><div style="font-weight:550">{{ r[0] }}</div><n-text depth="3">{{ r[3] }}</n-text></div>
          <span :style="MONO" style="font-weight:650;white-space:nowrap">\${{ fmt(r[1]) }}</span>
        </div>
      </n-space>
      <template #footer><n-text depth="3">每项均来自余额流水，可在「用户 → 余额流水」中逐笔核对。</n-text></template>
    </n-card>
    <n-card size="small" title="渠道消耗 · 近 7 天">
      <div v-if="pieTotal" style="display:flex;align-items:center;gap:16px">
        <Donut :items="pie" :center="'$' + fmt(pieTotal)" sub="实扣"/>
        <n-space vertical :size="6" style="flex:1;min-width:0">
          <n-tooltip v-for="p in pie" :key="p.label" placement="left">
            <template #trigger>
              <div class="legrow">
                <span class="legdot" :style="{background:p.color}"></span>
                <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;
                  white-space:nowrap">{{ p.label }}</span>
                <span :style="MONO" style="opacity:.6">
                  {{ Math.round(p.value / pieTotal * 100) }}%</span>
              </div>
            </template>
            <div style="font-size:11.5px">
              <div :style="MONO">{{ p.label }}</div>
              <div style="opacity:.75;margin-top:3px">
                {{ nf(p.requests) }} 次 · {{ kf(p.tokens) }} tok · {{ p.text }}</div>
            </div>
          </n-tooltip>
        </n-space>
      </div>
      <n-empty v-else description="近 7 天没有实扣" size="small" style="padding:20px 0"/>
    </n-card>
</div>

    <ModelCachePanel scope="site" default-range="week"/>
    <n-card class="console-table-card" size="small" title="消耗排行 · 近 7 天" style="margin-top:24px">
      <n-tabs type="line" size="small" animated>
        <n-tab-pane name="users" tab="用户">
          <n-data-table :columns="userCols" :data="d.top_users || []" size="small"
            :bordered="false" :scroll-x="640" :row-key="(r) => r.key"/>
        </n-tab-pane>
        <n-tab-pane name="models" tab="模型">
          <n-data-table :columns="aggCols('模型')" :data="d.by_model || []" size="small"
            :bordered="false" :scroll-x="640" :row-key="(r) => r.key"/>
        </n-tab-pane>
        <n-tab-pane name="channels" tab="渠道">
          <n-data-table :columns="aggCols('渠道')" :data="d.by_channel || []" size="small"
            :bordered="false" :scroll-x="640" :row-key="(r) => r.key"/>
        </n-tab-pane>
      </n-tabs>
    </n-card>
</Load>`,
  };

  /* ---------------- tab 渠道:代码渠道 + 数据渠道 ---------------- */

  /* 后端收结构化值也收文本;表单里用文本(多行 / a=b),回填时把对象拍平成文本。 */
  const kvText = (obj) => Object.keys(obj || {}).map((k) => k + "=" + obj[k]).join("\n");
  const blankChannel = () => ({ name: "", base_url: "", chat_path: "/chat/completions",
    models: "", model_map: "", headers: "", timeout: 0, key_concurrency: 0, notes: "" });

  const Channels = {
    components: { Load, AdminIntro },
    setup() {
      const rows = ref([]);
      const filter = ref("");
      const view = computed(() => {
        const value = filter.value.trim().toLowerCase();
        return value ? rows.value.filter((r) => [r.name, (r.models || []).join(" "),
          (r.config || {}).base_url || ""].join(" ").toLowerCase().includes(value)) : rows.value;
      });
      const poolSummary = computed(() => rows.value.reduce((out, r) => {
        out.active += Number((r.pool || {}).active || 0);
        out.total += Number((r.pool || {}).total || 0);
        out.online += r.disabled ? 0 : 1;
        return out;
      }, { active: 0, total: 0, online: 0 }));
      const editing = ref(null);        // null=关;{}=新建;带 name=编辑
      const isNew = ref(true);
      const form = reactive(blankChannel());
      const saving = ref(false);
      const busyCh = ref("");
      const keyDlg = ref(null);          // 正在导 key 的渠道名
      const keyText = ref("");
      const importing = ref(false);
      /* GPT 原生号池导入：解析与去重共用后端实现，逐条显示结果。 */
      const gptDlg = ref(false);
      const gptText = ref("");
      const gptForce = ref(false);
      const gptBusy = ref(false);
      const gptResult = ref(null);
      const gptFiles = ref([]);
      const gptFileInput = ref(null);
      const gptError = ref("");
      const testDlg = ref(null);         // 正在测试的渠道
      const testModel = ref(null);
      const testing = ref(false);
      const testResult = ref(null);

      const s = useFetch(() => api("GET", "/admin/channels").then((r) => {
        rows.value = r.channels || [];
      }));

      const openNew = () => {
        Object.assign(form, blankChannel());
        isNew.value = true;
        editing.value = {};
      };
      const openEdit = (r) => {
        const c = r.config || {};
        Object.assign(form, { name: r.name, base_url: c.base_url || "",
          chat_path: c.chat_path || "/chat/completions", models: (r.models || []).join("\n"),
          model_map: kvText(c.model_map), headers: kvText(c.headers),
          timeout: c.timeout || 0, key_concurrency: c.key_concurrency || 0,
          notes: c.notes || "" });
        isNew.value = false;
        editing.value = r;
      };
      const save = () => {
        const body = { base_url: form.base_url.trim(), chat_path: form.chat_path.trim(),
          models: form.models, model_map: form.model_map, headers: form.headers,
          timeout: Number(form.timeout) || 0,
          key_concurrency: Number(form.key_concurrency) || 0, notes: form.notes };
        if (isNew.value) body.name = form.name.trim();
        saving.value = true;
        (isNew.value ? api("POST", "/admin/channels", body)
          : api("PATCH", "/admin/channels/" + form.name, body))
          .then(() => {
            msg.success(isNew.value ? "渠道已创建,接着导入 key" : "渠道已更新,已生效");
            editing.value = null;
            return s.reload();
          })
          .catch((e) => msg.error(e.message))
          .then(() => { saving.value = false; });
      };
      const remove = (r) => dlg.warning({
        title: "删除渠道「" + r.name + "」",
        content: "会连同它号池里的 " + (r.pool.total || 0) + " 个账号一起删除,模型立刻从清单"
          + "与路由上消失。要临时停用请用「下线」,那是可逆的。",
        positiveText: "删除", negativeText: "取消",
        onPositiveClick: () => api("DELETE", "/admin/channels/" + r.name)
          .then((x) => { msg.success("已删除,清理账号 " + x.accounts_deleted + " 个"); return s.reload(); })
          .catch((e) => msg.error(e.message)),
      });
      /* 上下线与站点设置那张卡是同一个接口、同一份全量列表。 */
      const setOn = (r, on) => {
        const off = new Set(rows.value.filter((x) => x.disabled).map((x) => x.name));
        if (on) off.delete(r.name); else off.add(r.name);
        const apply = () => {
          busyCh.value = r.name;
          return api("PATCH", "/admin/settings", { disabled_channels: Array.from(off) })
            .then(() => { msg.success(r.name + (on ? " 已上线" : " 已下线")); return s.reload(); })
            .catch((e) => msg.error(e.message)).then(() => { busyCh.value = ""; });
        };
        if (on) return apply();
        dlg.warning({ title: "下线渠道「" + r.name + "」",
          content: "它的模型会立刻从模型清单、模型广场和 /v1/* 路由上消失,正在调用的客户端"
            + "会收到「未知模型」。号池与账号不受影响,随时能上回来。",
          positiveText: "下线", negativeText: "取消", onPositiveClick: apply });
      };
      const openKeys = (r) => { keyDlg.value = r.name; keyText.value = ""; };
      const doImport = () => {
        if (!keyText.value.trim()) return msg.warning("贴几把 key 进来,一行一把");
        importing.value = true;
        api("POST", "/admin/channels/" + keyDlg.value + "/keys", { key: keyText.value })
          .then((x) => {
            msg.success("导入 " + x.imported + " 把,跳过重复 " + x.skipped + " 把;号池现有 "
              + (x.pool.active || 0) + " 把可用");
            keyDlg.value = null;
            return s.reload();
          })
          .catch((e) => msg.error(e.message)).then(() => { importing.value = false; });
      };
      const openTest = (r) => {
        testDlg.value = r;
        testModel.value = (r.models || [])[0] || null;
        testResult.value = null;
      };
      const doTest = () => {
        testing.value = true;
        testResult.value = null;
        api("POST", "/admin/channels/" + testDlg.value.name + "/test", { model: testModel.value })
          .then((x) => { testResult.value = x; })
          .catch((e) => { testResult.value = { ok: false, error: e.message }; })
          .then(() => { testing.value = false; });
      };

      const openGPT = () => {
        gptDlg.value = true;
        gptText.value = "";
        gptForce.value = false;
        gptResult.value = null;
        gptFiles.value = [];
        gptError.value = "";
      };
      const readGPTFiles = async (event) => {
        const files = Array.from(event.target.files || []);
        event.target.value = "";
        if (!files.length || gptBusy.value) return;
        const total = files.reduce((sum, f) => sum + f.size, 0);
        if (total > 10 * 1024 * 1024) return msg.error("文件合计不能超过 10 MB");
        gptBusy.value = true;
        gptError.value = "";
        try {
          gptFiles.value = await Promise.all(files.map(async (f) =>
            ({ name: f.name, content: await f.text() })));
          gptResult.value = null;
        } catch (e) { gptError.value = "读取文件失败: " + e.message; }
        finally { gptBusy.value = false; }
      };
      const doGPTImport = () => {
        if (gptBusy.value) return;
        if (!gptText.value.trim() && !gptFiles.value.length)
          return msg.warning("粘贴 accessToken / auth.json，或选择导入文件");
        gptBusy.value = true;
        gptResult.value = null;
        gptError.value = "";
        api("POST", "/admin/gpt/import",
            { content: gptText.value, contents: gptFiles.value.map((f) => f.content),
              force: gptForce.value })
          .then((x) => {
            gptResult.value = x;
            const bits = [];
            if (x.imported) bits.push("新增 " + x.imported);
            if (x.updated) bits.push("更新 " + x.updated);
            if (x.skipped) bits.push("跳过 " + x.skipped);
            if (x.failed) bits.push("失败 " + x.failed);
            const summary = "导入完成:" + (bits.join(" · ") || "没有可导入的条目");
            if (x.failed === x.total) msg.error(summary);
            else if (x.failed) msg.warning(summary);
            else msg.success(summary);
            return s.reload();
          })
          .catch((e) => { gptError.value = e.message; msg.error(e.message); })
          .then(() => { gptBusy.value = false; });
      };

      /* 优先级 / 权重就地改,停手 500ms 再发:两个数字框敲一位发一次会把注册表刷得
         闪个不停。同一渠道的两次改动合并成一次 PATCH。 */
      const routingTimers = {};
      const setRouting = (r, field, v) => {
        if (v === null || v === undefined) return;
        r[field] = v;
        clearTimeout(routingTimers[r.name]);
        routingTimers[r.name] = setTimeout(() => {
          api("PATCH", "/admin/channels/" + r.name + "/routing",
              { priority: r.priority, weight: r.weight })
            .then(() => msg.success(r.name + " 路由已更新:优先级 " + r.priority +
                                    " · 权重 " + r.weight))
            .catch((e) => { msg.error(e.message); s.reload(); });
        }, 500);
      };
      const poolText = (p) => {
        const parts = [];
        if (p.active) parts.push(p.active + " 可用");
        if (p.cooldown) parts.push(p.cooldown + " 冷却");
        if (p.exhausted) parts.push(p.exhausted + " 耗尽");
        if (p.dead) parts.push(p.dead + " 失效");
        if (p.unchecked) parts.push(p.unchecked + " 待验");
        return parts.length ? parts.join(" · ") : "空";
      };
      const canTest = (r) => r.kind !== "generation" && !r.proxy &&
        (r.capabilities || []).indexOf("chat") >= 0;
      const cols = [
        { title: "渠道", key: "name", minWidth: 200, render: (r) => stack(
            h(naive.NSpace, { size: 5, align: "center" }, () => [
              h("span", { style: Object.assign({ fontWeight: 600 }, MONO) }, r.name),
              tag(r.source === "data" ? "info" : "default", r.source === "data" ? "数据" : "代码"),
              r.disabled ? tag("warning", "已下线") : null,
              r.kind === "generation" ? tag("default", "生成") : null,
              r.proxy ? tag("default", "转发") : null]),
            r.source === "data" ? ((r.config || {}).base_url || "")
              : (r.streaming ? "SSE 透传 · " : "") + (r.billing_mode || "")) },
        { title: "模型", key: "models", width: 130, render: (r) => {
            const shared = new Set(r.shared_models || []);
            return h(naive.NPopover, { trigger: "hover", placement: "bottom" }, {
              trigger: () => stack(
                h(naive.NButton, { text: true, size: "tiny", style: MONO },
                  () => (r.models || []).length + " 个"),
                shared.size ? shared.size + " 个多渠道" : null),
              /* 与别的渠道共享的模型标出来:站长要知道哪些模型有备胎、哪些是独苗 */
              default: () => h("div", { style: Object.assign({ fontSize: "12px",
                maxHeight: "260px", overflow: "auto" }, MONO) },
                (r.models || []).map((m) => h("div", { style: {
                  color: shared.has(m) ? "#4d7fa0" : undefined } },
                  m + (shared.has(m) ? "  · 多渠道" : "")))) }); } },
        { title: "优先级 / 权重", key: "priority", width: 168, render: (r) =>
            h(naive.NSpace, { size: 4, wrap: false, align: "center" }, () => [
              h(naive.NInputNumber, { size: "tiny", value: r.priority, min: -1000, max: 1000,
                showButton: false, style: { width: "64px" },
                onUpdateValue: (v) => setRouting(r, "priority", v) }),
              h(naive.NInputNumber, { size: "tiny", value: r.weight, min: 0, max: 1000,
                showButton: false, style: { width: "64px" },
                onUpdateValue: (v) => setRouting(r, "weight", v) })]) },
        { title: "号池", key: "pool", minWidth: 190, render: (r) => stack(
            h("span", { style: Object.assign({ fontWeight: 600,
              color: (r.pool.active || 0) ? undefined : C.err }, MONO) },
              nf(r.pool.active || 0) + " / " + nf(r.pool.total || 0)),
            poolText(r.pool)) },
        { title: "状态", key: "disabled", width: 90, render: (r) =>
            h(naive.NSwitch, { size: "small", value: !r.disabled, loading: busyCh.value === r.name,
              onUpdateValue: (v) => setOn(r, v) }) },
        { title: "操作", key: "act", width: 300, fixed: "right", render: (r) =>
            h(naive.NSpace, { size: 6 }, () => [
              r.name === "gpt"
                ? h(naive.NButton, { size: "tiny", secondary: true, onClick: openGPT },
                    () => "导入账号")
                : h(naive.NButton, { size: "tiny", secondary: true, onClick: () => openKeys(r) },
                    () => "导入 key"),
              canTest(r) ? h(naive.NButton, { size: "tiny", secondary: true,
                onClick: () => openTest(r) }, () => "测试") : null,
              r.source === "data" ? h(naive.NButton, { size: "tiny", secondary: true,
                onClick: () => openEdit(r) }, () => "编辑") : null,
              r.source === "data" ? h(naive.NButton, { size: "tiny", secondary: true,
                type: "error", onClick: () => remove(r) }, () => "删除") : null,
            ]) },
      ];
      return Object.assign({ rows, cols, editing, isNew, form, saving, save, openNew, filter, view, poolSummary,
        keyDlg, keyText, importing, doImport, testDlg, testModel, testing, testResult,
        doTest, nf, MONO,
        gptDlg, gptText, gptForce, gptBusy, gptResult, gptFiles, gptFileInput, gptError,
        openGPT, doGPTImport, readGPTFiles,
        gptAction: (action) => ({ imported: "新增", updated: "更新", skipped: "跳过", failed: "失败" }[action] || action),
        testOpts: computed(() => ((testDlg.value || {}).models || []).map(
          (m) => ({ label: m, value: m }))) }, s);
    },
    template: `
<Load :loading="loading" :error="error" :on-retry="reload">
<AdminIntro title="渠道与资源" note="查看上游模型、路由顺序与账号库存。先配置渠道，再导入密钥、测试请求并设置模型价格。">
  <n-button secondary @click="openGPT">导入 GPT 账号</n-button>
  <n-button type="primary" @click="openNew">新建兼容渠道</n-button>
</AdminIntro>
<div class="console-resource-strip">
  <div class="console-inline-stat"><span class="console-inline-stat-value">{{ poolSummary.online }} / {{ rows.length }}</span><span class="console-inline-stat-label">已上线 / 全部渠道</span></div>
  <div class="console-inline-stat"><span class="console-inline-stat-value">{{ nf(poolSummary.active) }} / {{ nf(poolSummary.total) }}</span><span class="console-inline-stat-label">号池可用 / 总账号 · 含下线渠道</span></div>
  <div class="console-inline-stat"><span class="console-inline-stat-value">{{ rows.filter(r => r.source === 'data').length }}</span><span class="console-inline-stat-label">控制台创建的渠道</span></div>
</div>
<n-card class="console-table-card" size="small" title="渠道列表">
  <div class="console-toolbar">
    <n-input v-model:value="filter" clearable placeholder="搜索渠道、模型或上游地址" style="width:320px;max-width:100%"/>
    <n-space align="center"><n-text depth="3">{{ view.length }} / {{ rows.length }} 个渠道</n-text><n-button secondary :loading="loading" @click="reload">刷新</n-button></n-space>
  </div>
  <n-data-table :columns="cols" :data="view" size="small" :bordered="false"
    :single-line="false" :scroll-x="1180" :row-key="(r) => r.name"/>
  <n-text depth="3" style="font-size:11.5px;display:block;margin-top:10px">
    数据渠道存库即生效、不用重启:建好 → 导入 key → 测试 → 到「模型定价」给模型配价。
    优先级与权重修改后直接保存。同一模型可以挂多个渠道:优先级高的先试,没号或换遍号仍失败就降到下一档,同档按权重分流
    (权重 0 只作兜底)。下线是可逆的,删除会连号池一起清。
  </n-text>
</n-card>

<n-drawer :show="!!editing" width="min(560px, 100vw)" placement="right"
  @update:show="(v) => { if (!v) editing = null; }">
  <n-drawer-content v-if="editing" :title="isNew ? '新建 OpenAI 兼容渠道' : '编辑渠道 ' + form.name"
    closable :native-scrollbar="false">
    <n-space vertical :size="14">
      <n-form-item v-if="isNew" label="渠道名" :show-feedback="false" label-placement="top">
        <n-input v-model:value="form.name" placeholder="小写字母/数字/-/_,建后不可改"/>
      </n-form-item>
      <n-form-item label="上游地址(base_url)" :show-feedback="false" label-placement="top">
        <n-input v-model:value="form.base_url" placeholder="https://api.example.com/v1"/>
      </n-form-item>
      <n-form-item label="对话路径" :show-feedback="false" label-placement="top">
        <n-input v-model:value="form.chat_path" placeholder="/chat/completions"/>
      </n-form-item>
      <n-form-item label="模型清单(一行一个,对外名)" :show-feedback="false" label-placement="top">
        <n-input v-model:value="form.models" type="textarea" :autosize="{minRows:3,maxRows:10}"
          placeholder="gpt-4.1-mini&#10;claude-sonnet-4"/>
      </n-form-item>
      <n-form-item label="模型映射(可空,对外名=上游名)" :show-feedback="false" label-placement="top">
        <n-input v-model:value="form.model_map" type="textarea" :autosize="{minRows:2,maxRows:8}"
          placeholder="gpt-4.1-mini=gpt-4.1-mini-2025-04-14"/>
      </n-form-item>
      <n-form-item label="额外请求头(可空,名=值)" :show-feedback="false" label-placement="top">
        <n-input v-model:value="form.headers" type="textarea" :autosize="{minRows:1,maxRows:6}"
          placeholder="X-Title=bit-api"/>
      </n-form-item>
      <n-grid :cols="2" :x-gap="12">
        <n-gi><n-form-item label="超时(秒,0=默认)" :show-feedback="false" label-placement="top">
          <n-input-number v-model:value="form.timeout" :min="0" :max="3600" style="width:100%"/>
        </n-form-item></n-gi>
        <n-gi><n-form-item label="每号并发(0=不限)" :show-feedback="false" label-placement="top">
          <n-input-number v-model:value="form.key_concurrency" :min="0" :max="1000" style="width:100%"/>
        </n-form-item></n-gi>
        <n-gi><n-form-item label="备注" :show-feedback="false" label-placement="top">
          <n-input v-model:value="form.notes" placeholder="可空"/>
        </n-form-item></n-gi>
      </n-grid>
      <n-text depth="3" style="font-size:11.5px">
        鉴权头由渠道自己生成(Bearer 每把 key);流式原样透传上游 SSE,tool_calls 与 usage 不丢;
        上游 401/402/403 让 key 退场,其余抖动只轮换不罚号。
      </n-text>
      <n-space justify="end">
        <n-button @click="editing = null">取消</n-button>
        <n-button type="primary" :loading="saving" @click="save">{{ isNew ? '创建' : '保存' }}</n-button>
      </n-space>
    </n-space>
  </n-drawer-content>
</n-drawer>

<n-modal :show="!!keyDlg" preset="card" :title="'导入 key → ' + keyDlg" style="width:min(520px, calc(100vw - 32px))"
  @update:show="(v) => { if (!v) keyDlg = null; }">
  <n-input v-model:value="keyText" type="textarea" :autosize="{minRows:5,maxRows:14}"
    placeholder="一行一把,也可逗号分隔;重复的会跳过" :style="MONO"/>
  <template #footer><n-space justify="end">
    <n-button @click="keyDlg = null">取消</n-button>
    <n-button type="primary" :loading="importing" @click="doImport">导入</n-button>
  </n-space></template>
</n-modal>

<n-modal :show="gptDlg" preset="card" title="导入 GPT / Codex 账号"
  style="width:min(660px, calc(100vw - 32px))"
  :mask-closable="!gptBusy" :close-on-esc="!gptBusy" :closable="!gptBusy"
  @update:show="(v) => { if (!v) gptDlg = false; }">
  <n-space vertical :size="12">
    <input ref="gptFileInput" type="file" accept=".json,.jsonl,.txt" multiple
      style="display:none" @change="readGPTFiles"/>
    <n-space align="center">
      <n-button secondary :disabled="gptBusy" @click="gptFileInput.click()">选择文件</n-button>
      <n-text depth="3">支持多份 auth.json、JSON 数组、JSONL 或文本；合计最多 10 MB</n-text>
    </n-space>
    <n-space v-if="gptFiles.length" :size="6">
      <n-tag v-for="(f, i) in gptFiles" :key="i" :closable="!gptBusy"
        @close="gptFiles.splice(i, 1)">{{ f.name }}</n-tag>
    </n-space>
    <n-input v-model:value="gptText" type="textarea" :autosize="{minRows:8,maxRows:18}"
      :disabled="gptBusy"
      :style="MONO"
      placeholder='一行一个 accessToken,或者直接贴 Codex 的 auth.json:&#10;{"tokens":{"access_token":"eyJ...","refresh_token":"...","id_token":"eyJ..."},"last_refresh":"2026-..."}&#10;也接受 JSON 数组 [{"access_token":"eyJ..."}, ...]'/>
    <n-checkbox v-model:checked="gptForce" :disabled="gptBusy">
      覆盖已有账号（默认跳过；仅需更新凭据时勾选）
    </n-checkbox>
    <n-text depth="3" style="font-size:11.5px">
      自动提取邮箱、套餐与账号 ID，支持 Codex / ChatGPT session / sub2api 导出格式。
      凭据保存到 bit-api 账号数据库，由本站自动续期、轮换和直连 GPT。
      导入成功后请点 gpt 渠道「测试」确认可用。
    </n-text>
    <n-alert v-if="gptError" type="error" :bordered="false">{{ gptError }}</n-alert>
    <n-alert v-if="gptResult" :type="gptResult.failed === gptResult.total ? 'error' : gptResult.failed ? 'warning' : 'success'"
      :bordered="false" :show-icon="false">
      共 {{ gptResult.total }} 条:新增 {{ gptResult.imported }} · 更新 {{ gptResult.updated }}
      · 跳过 {{ gptResult.skipped }} · 失败 {{ gptResult.failed }}
      <div v-if="gptResult.items && gptResult.items.length"
        style="margin-top:8px;max-height:220px;overflow:auto" :style="MONO">
        <div v-for="it in gptResult.items" :key="it.index" style="font-size:12px">
          #{{ it.index }} {{ it.name || '' }} · {{ gptAction(it.action) }}
          <span v-if="it.message" style="opacity:.75">— {{ it.message }}</span>
        </div>
      </div>
    </n-alert>
    <n-alert v-if="gptResult && gptResult.warnings && gptResult.warnings.length"
      type="warning" :bordered="false" title="导入提示">
      <div style="max-height:180px;overflow:auto;font-size:12px">
        <div v-for="(w, i) in gptResult.warnings" :key="i">#{{ w.index }} {{ w.name || '' }}：{{ w.message }}</div>
      </div>
    </n-alert>
  </n-space>
  <template #footer><n-space justify="end">
    <n-button :disabled="gptBusy" @click="gptDlg = false">关闭</n-button>
    <n-button type="primary" :loading="gptBusy" @click="doGPTImport">导入</n-button>
  </n-space></template>
</n-modal>

<n-modal :show="!!testDlg" preset="card" :title="'测试渠道 ' + (testDlg && testDlg.name)"
  style="width:min(520px, calc(100vw - 32px))" @update:show="(v) => { if (!v) testDlg = null; }">
  <n-space vertical :size="12">
    <n-select v-model:value="testModel" :options="testOpts" filterable/>
    <n-alert v-if="testResult" :type="testResult.ok ? 'success' : 'error'" :bordered="false"
      :show-icon="false">
      <template v-if="testResult.ok">
        {{ testResult.elapsed_ms }} ms · 账号 {{ testResult.identity }}
        <div :style="MONO" style="margin-top:6px;font-size:12.5px">{{ testResult.reply || '(空回复)' }}</div>
        <div v-if="testResult.usage" style="opacity:.7;font-size:11.5px;margin-top:4px">
          usage:{{ testResult.usage.prompt_tokens }} / {{ testResult.usage.completion_tokens }}</div>
      </template>
      <template v-else>
        <div :style="MONO" style="font-size:12.5px;word-break:break-all">{{ testResult.error }}</div>
      </template>
    </n-alert>
    <n-text depth="3" style="font-size:11.5px">
      从号池取一把号发一句话,不计费。失败会显示上游状态码与响应体原话;401/402/403 会让这把号退场。
    </n-text>
  </n-space>
  <template #footer><n-space justify="end">
    <n-button @click="testDlg = null">关闭</n-button>
    <n-button type="primary" :loading="testing" @click="doTest">发送测试请求</n-button>
  </n-space></template>
</n-modal>
</Load>`,
  };

  /* ---------------- tab 日志:全站用量明细 ---------------- */

  const LOG_PAGE = 50;

  const Logs = {
    components: { Load, Pills, AdminIntro },
    setup() {
      const rows = ref([]);
      const total = ref(0);
      const page = ref(1);
      const pageSize = ref(LOG_PAGE);
      const chans = ref([]);
      const detail = ref(null);
      const exporting = ref(false);
      const f = reactive({ email: "", model: "", channel: "all", span: null,
        failed: false });

      const params = () => {
        const span = f.span && f.span.length === 2 ? f.span : null;
        return { email: f.email.trim(), model: f.model.trim(), channel: f.channel,
          since: span ? Math.floor(span[0] / 1000) : null,
          until: span ? Math.ceil(span[1] / 1000) : null,
          end_reason: f.failed ? "failed" : null };
      };
      const s = useFetch(() => Promise.all([
        api("GET", "/admin/usage" + qs(Object.assign(params(), {
          limit: pageSize.value, offset: (page.value - 1) * pageSize.value }))),
        chans.value.length ? Promise.resolve({ channels: chans.value })
          : api("GET", "/admin/channels"),
      ]).then((r) => {
        rows.value = (r[0].logs || []).map((x) => {
          const n = normUsage(x, null);
          n.email = x.email || ("#" + x.user_id);
          n.userId = x.user_id;
          n.key = x.key_name || (x.api_key_id ? "#" + x.api_key_id : "体验页");
          return n;
        });
        total.value = r[0].total || 0;
        chans.value = r[1].channels || [];
      }));
      const query = () => { page.value = 1; return s.reload(); };
      const jump = (n) => { page.value = n; s.reload(); };
      const resize = (n) => { pageSize.value = n; page.value = 1; s.reload(); };
      /* 下拉与勾选即改即查;两个文本框要等回车或点查询,不然每敲一个字母打一次库。 */
      watch(() => [f.channel, f.failed,
        f.span && f.span.length === 2 ? f.span.join() : ""].join("|"), query);
      const reset = () => {
        f.email = ""; f.model = ""; f.channel = "all"; f.span = null; f.failed = false;
        query();
      };
      const exportCsv = () => {
        exporting.value = true;
        download("/admin/usage/export.csv" + qs(params()), "site-usage.csv")
          .then(() => msg.success("已导出当前条件下的记录(单次最多 10000 条)"))
          .catch((e) => msg.error(e.message))
          .then(() => { exporting.value = false; });
      };
      const chanOpts = computed(() => [{ label: "全部渠道", value: "all" }]
        .concat(chans.value.map((c) => ({ label: c.name, value: c.name }))));
      const focusUser = (r) => { f.email = r.email.indexOf("#") === 0 ? "" : r.email; query(); };

      const cols = usageColumns((r) => { detail.value = r; }, focusUser);
      const rowClass = (r) => (!r.ok ? "row-err" : "");
      return Object.assign({ rows, total, page, pageSize, f, chanOpts, cols, rowClass,
        detail, jump, resize, query, reset, exportCsv, exporting, nf, fmt, usd, MONO,
        absTime, END_TEXT, tpsOf,
        copyRid: () => detail.value && detail.value.rid
          ? copy(detail.value.rid, "已复制请求 ID") : msg.warning("该记录没有请求 ID"),
      }, s);
    },
    template: `
<AdminIntro title="全站日志" note="按用户、模型与时间定位请求，打开详情核对计量、费用与结束原因。日志不保存请求正文。">
  <n-button secondary :loading="exporting" @click="exportCsv">导出 CSV</n-button>
</AdminIntro>
<n-card class="console-table-card" size="small" title="查询请求" :segmented="{content:true}"
        :content-style="'padding:14px 15px'">
  <template #header-extra>
    <n-text depth="3" style="font-size:11.5px">共 {{ nf(total) }} 条</n-text>
  </template>
  <div class="admin-log-filters">
    <n-form-item label="用户邮箱" label-placement="top" :show-feedback="false"><n-input v-model:value="f.email" clearable placeholder="精确匹配邮箱"
      @keyup.enter="query" @clear="query"/></n-form-item>
    <n-form-item label="模型" label-placement="top" :show-feedback="false"><n-input v-model:value="f.model" clearable placeholder="模型名，支持 kg-*"
      @keyup.enter="query" @clear="query"/></n-form-item>
    <n-form-item label="渠道" label-placement="top" :show-feedback="false"><n-select v-model:value="f.channel" :options="chanOpts"/></n-form-item>
    <n-form-item class="admin-log-range" label="时间范围" label-placement="top" :show-feedback="false"><n-date-picker v-model:value="f.span" type="datetimerange" clearable
      style="width:100%"/></n-form-item>
    <n-form-item label="结束状态" label-placement="top" :show-feedback="false"><n-checkbox v-model:checked="f.failed">只看未正常结束</n-checkbox></n-form-item>
  </div>
  <n-space justify="end" :size="7" style="margin-top:10px">
    <n-button size="small" secondary @click="reset">重置</n-button>
    <n-button size="small" type="primary" :loading="loading" @click="query">查询</n-button>
  </n-space>
  <template #footer>
    <Load :loading="loading" :error="error" :on-retry="reload">
      <n-data-table class="usage-table" table-layout="fixed" :columns="cols" :data="rows" :bordered="false" size="small"
        :row-class-name="rowClass" :scroll-x="1456" :row-key="(r) => r.id"/>
      <n-space justify="space-between" align="center" style="margin-top:10px" wrap>
        <n-text depth="3" style="font-size:11.5px">
          点用户名可只看该用户；未选时间时查询全部时间范围。筛选条件在翻页时保持。
        </n-text>
        <n-pagination class="usage-pagination" :page="page" :page-size="pageSize" :item-count="total" :page-slot="6"
          show-size-picker :page-sizes="[50,100,200,500]"
          @update:page="jump" @update:page-size="resize"/>
      </n-space>
    </Load>
  </template>
</n-card>

<n-drawer class="usage-drawer" :show="!!detail" width="min(440px, 100vw)" placement="right"
  @update:show="(v) => { if (!v) detail = null; }">
  <n-drawer-content v-if="detail" title="请求详情" closable>
    <n-space vertical :size="12">
      <n-alert v-if="!detail.ok" type="error" :bordered="false" :show-icon="false"
        title="请求未正常结束">{{ END_TEXT[detail.end] || detail.end }}</n-alert>
      <n-descriptions :column="1" label-placement="left" size="small"
        :label-style="{opacity:.62,width:'84px',whiteSpace:'nowrap'}">
        <n-descriptions-item label="时间"><span :style="MONO">{{ absTime(detail.t) }}</span></n-descriptions-item>
        <n-descriptions-item label="请求 IP"><span :style="MONO">{{ detail.ip || '未记录' }}</span></n-descriptions-item>
        <n-descriptions-item label="用户">{{ detail.email }} (#{{ detail.userId }})</n-descriptions-item>
        <n-descriptions-item label="密钥">{{ detail.key }}</n-descriptions-item>
        <n-descriptions-item label="模型 / 渠道"><code :style="MONO">{{ detail.model }}</code> · {{ detail.channel || '—' }}</n-descriptions-item>
        <n-descriptions-item label="请求 ID"><span :style="MONO" style="font-size:12px">{{ detail.rid || '—' }}</span></n-descriptions-item>
        <n-descriptions-item label="Token">输入 {{ nf(detail.i) }} · 输出 {{ nf(detail.o) }}
          · 计量 {{ detail.tokenSource || '—' }}</n-descriptions-item>
        <n-descriptions-item label="缓存读取">{{ detail.cacheRead == null ? '未记录' : nf(detail.cacheRead) }}</n-descriptions-item>
        <n-descriptions-item label="缓存写入">{{ detail.cacheWrite == null ? '未记录' : nf(detail.cacheWrite) }}</n-descriptions-item>
        <n-descriptions-item v-if="detail.cacheWrite30m != null" label="写入 · 30 分钟">
          {{ nf(detail.cacheWrite30m) }} token<span v-if="detail.px?.cw30 != null"> · {{ usd(detail.px.cw30) }} /M</span></n-descriptions-item>
        <n-descriptions-item v-if="detail.cacheWrite12h != null" label="写入 · 12 小时">
          {{ nf(detail.cacheWrite12h) }} token<span v-if="detail.px?.cw12 != null"> · {{ usd(detail.px.cw12) }} /M</span></n-descriptions-item>
        <n-descriptions-item v-if="detail.cacheWriteUnclassified" label="写入 · 未分档">
          {{ nf(detail.cacheWriteUnclassified) }} token · 基础写入价</n-descriptions-item>
        <n-descriptions-item v-if="detail.cacheWriteTTLValid === false" label="写入分档">
          上游分档不完整或合计不符，按合计和基础写入价结算</n-descriptions-item>
        <n-descriptions-item v-if="!detail.cacheSplit && detail.cache != null" label="历史缓存">
          合计 {{ nf(detail.cache) }} token，未保存读写明细</n-descriptions-item>
        <n-descriptions-item label="费用">实扣 {{ usd(detail.cost) }} · 原价 {{ usd(detail.list) }}
          <template v-if="detail.ratio != null && detail.ratio !== 1"> · 倍率 {{ detail.ratio }}x</template>
        </n-descriptions-item>
        <n-descriptions-item label="耗时">{{ (detail.ms/1000).toFixed(2) }}s
          <template v-if="detail.frt != null"> · 首字 {{ (detail.frt/1000).toFixed(2) }}s</template>
          <template v-if="tpsOf(detail)"> · {{ tpsOf(detail) }} t/s</template>
        </n-descriptions-item>
        <n-descriptions-item label="策略 / 模式">{{ detail.policy || '—' }} / {{ detail.mode }}
          <template v-if="detail.tier === 'long'"> · 长上下文档</template></n-descriptions-item>
      </n-descriptions>
      <n-button size="small" secondary @click="copyRid">复制请求 ID</n-button>
    </n-space>
  </n-drawer-content>
</n-drawer>`,
  };

  /* ---------------- tab 模型状态 ---------------- */


  const Admin = {
    props: ["selectedTab"],
    components: { Settings: Settings, Groups: Groups, Pricing: Pricing,
      Users: Users, Invites: Invites, Codes: Codes,
      Announcements: Announcements, Stats: Stats, Logs: Logs, Channels: Channels },
    // Pool 由后加载的 portal-pool.js 在 BitPortalPool 就绪后注册。
    setup(props) {
      return { tab: computed(() => props.selectedTab || "stats") };
    },
    template: `
<div class="console-admin-view" :key="tab">
<Stats v-if="tab === 'stats'"/>
<Logs v-else-if="tab === 'logs'"/>
<Channels v-else-if="tab === 'channels'"/>
<Pool v-else-if="tab === 'pool'"/>
<Settings v-else-if="tab === 'settings'"/>
<Announcements v-else-if="tab === 'announcements'"/>
<Groups v-else-if="tab === 'groups'"/>
<Pricing v-else-if="tab === 'pricing'"/>
<Users v-else-if="tab === 'users'"/>
<Invites v-else-if="tab === 'invites'"/>
<Codes v-else-if="tab === 'codes'"/>
<n-text v-else depth="3">未知的管理页:{{ tab }}</n-text>
</div>`,
  };

  window.BitPortalAdmin = { Admin: Admin, Settings: Settings, Groups: Groups,
    Pricing: Pricing, Users: Users, Invites: Invites, Codes: Codes,
    Announcements: Announcements, Stats: Stats, Logs: Logs, Channels: Channels,
    AdminIntro: AdminIntro };
})();
