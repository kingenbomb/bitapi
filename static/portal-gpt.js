/* GPT account controls. All irreversible upstream actions require a fresh preview. */
(function () {
  "use strict";
  const { reactive, ref, watch, onMounted } = Vue;
  const { api, msg, absTime, Ic } = window.BitPortal;
  const root = "/admin/pool";
  const defaults = { pause_5h: 1, pause_7d: 1, auto_reset: false, reset_5h: 1, reset_7d: 1,
    model_whitelist: [], model_mapping: {}, compact_model_mapping: {}, cli_only: false, ws_mode: "off",
    passthrough: true, flatten_namespaces: false, compact_mode: "auto", image_tool: "auto",
    fingerprint_mode: "canonical", header_overrides: {}, plan_override: null };
  const Settings = {
    props: ["value", "shadow"], emits: ["update:value"], components: { Ic },
    setup(props, { emit }) {
      const cfg = reactive({ ...defaults, ...JSON.parse(JSON.stringify(props.value || {})) });
      const text = reactive({ mapping: JSON.stringify(cfg.model_mapping, null, 2), compact: JSON.stringify(cfg.compact_model_mapping, null, 2), headers: JSON.stringify(cfg.header_overrides, null, 2) });
      const error = ref("");
      const publish = () => {
        try {
          for (const [key, input] of [["model_mapping", "mapping"], ["compact_model_mapping", "compact"], ["header_overrides", "headers"]]) {
            const parsed = JSON.parse(text[input] || "{}");
            if (!parsed || Array.isArray(parsed) || typeof parsed !== "object" || Object.values(parsed).some(v => typeof v !== "string")) throw Error("映射与请求头需要 JSON 对象，值必须是文本");
            cfg[key] = parsed;
          }
          error.value = ""; emit("update:value", JSON.parse(JSON.stringify(cfg)));
        } catch (e) { error.value = e.message; emit("update:value", { ...cfg, model_mapping: { "": "invalid JSON" } }); }
      };
      return { cfg, text, error, publish };
    },
    template: `<div class="pool-gpt-settings">
      <div class="pool-section-heading"><h3><Ic name="gauge" :size="17"/>GPT 账号策略</h3></div>
      <div class="pool-form-grid">
        <n-form-item v-for="key in ['pause_5h','pause_7d']" :key="key" :label="(key==='pause_5h'?'5 小时':'7 天')+'暂停阈值（已用 %）'"><n-input-number :value="cfg[key]==null?null:Math.round(cfg[key]*100)" :min="1" :max="100" placeholder="留空关闭阈值暂停" @update:value="v=>{cfg[key]=v==null?null:v/100;publish()}"/></n-form-item>
        <n-form-item label="自动使用重置券" class="pool-span-2"><n-switch v-model:value="cfg.auto_reset" :disabled="!!shadow" @update:value="publish"/><span class="pool-field-hint">开启后达到阈值会消耗真实上游券；不解除管理员停用。</span></n-form-item>
        <template v-if="cfg.auto_reset"><n-alert type="warning" class="pool-span-2" :show-icon="false">保存即授权本账号自动消耗重置券。每次消耗前会重新读取上游额度；结果未知时暂停自动执行，等待你核对。</n-alert><n-form-item v-for="key in ['reset_5h','reset_7d']" :key="key" :label="(key==='reset_5h'?'5 小时':'7 天')+'自动重置阈值 %'"><n-input-number :value="Math.round(cfg[key]*100)" :min="1" :max="100" @update:value="v=>{cfg[key]=(v||100)/100;publish()}"/></n-form-item></template>
        <n-form-item label="模型白名单" class="pool-span-2"><n-dynamic-tags v-model:value="cfg.model_whitelist" @update:value="publish"/><p class="pool-field-hint">留空使用上游可见模型；影响模型发现和实际取号。可输入模型 ID。</p></n-form-item>
        <n-form-item label="模型映射 · 对外 ID → 上游 ID"><n-input v-model:value="text.mapping" type="textarea" :autosize="{minRows:3,maxRows:7}" @update:value="publish"/></n-form-item>
        <n-form-item label="压缩模型映射 · 对外 ID → 上游 ID"><n-input v-model:value="text.compact" type="textarea" :autosize="{minRows:3,maxRows:7}" @update:value="publish"/></n-form-item>
        <n-form-item label="WebSocket v2"><n-select v-model:value="cfg.ws_mode" :options="[{label:'关闭',value:'off'},{label:'跨轮重连 · 完整上下文',value:'ctx_pool'},{label:'保持上游连接',value:'passthrough'}]" @update:value="publish"/></n-form-item>
        <n-form-item label="上下文压缩"><n-select v-model:value="cfg.compact_mode" :options="[{label:'跟随客户端',value:'auto'},{label:'允许',value:'enabled'},{label:'禁用',value:'disabled'}]" @update:value="publish"/></n-form-item>
        <n-form-item label="原生图片工具"><n-select v-model:value="cfg.image_tool" :options="[{label:'跟随客户端',value:'auto'},{label:'自动附加工具',value:'enabled'},{label:'禁用',value:'disabled'}]" @update:value="publish"/></n-form-item>
        <n-form-item label="请求头指纹"><n-select v-model:value="cfg.fingerprint_mode" :options="[{label:'标准 Codex',value:'canonical'},{label:'固定账号标识',value:'session'},{label:'透传客户端标识',value:'passthrough'}]" @update:value="publish"/></n-form-item>
        <n-form-item label="客户端与协议" class="pool-span-2"><n-space><n-checkbox v-model:checked="cfg.cli_only" @update:checked="publish">仅 Codex 客户端</n-checkbox><n-checkbox v-model:checked="cfg.passthrough" @update:checked="publish">保留原生扩展字段</n-checkbox><n-checkbox v-model:checked="cfg.flatten_namespaces" @update:checked="publish">展开工具命名空间</n-checkbox></n-space></n-form-item>
        <n-form-item label="自定义请求头（JSON）"><n-input v-model:value="text.headers" type="textarea" :autosize="{minRows:2,maxRows:6}" @update:value="publish"/></n-form-item>
        <n-form-item label="套餐显示覆盖"><n-input v-model:value="cfg.plan_override" clearable placeholder="留空使用上游套餐" @update:value="publish"/></n-form-item>
      </div><n-alert v-if="error" type="error" :show-icon="false">{{error}}</n-alert>
    </div>`
  };
  const Operations = {
    props: ["row", "resetOnly"], emits: ["saved", "close"], components: { Ic },
    setup(props, { emit }) {
      const state = reactive({ busy: "", error: "", history: [], referral: null, email: "", consent: false,
        review: null, confirmed: false, result: null, spark: [], sparkName: "", ack: null });
      const path = () => root + "/accounts/" + props.row.id;
      const run = async (name, fn) => { state.busy = name; state.error = ""; try { await fn(); } catch (e) { state.error = e.message; } finally { state.busy = ""; } };
      const history = async () => { state.history = (await api("GET", path() + "/gpt-actions")).history || []; };
      onMounted(() => props.resetOnly ? preview("reset") : run("history", history));
      const preview = action => run("preview", async () => {
        state.review = await api("POST", path() + "/gpt-actions/preview", { action, ...(action === 'referral' ? { email: state.email } : {}) });
        state.confirmed = false; state.result = null;
      });
      const execute = () => run("execute", async () => {
        state.result = await api("POST", path() + "/gpt-actions/execute", { request_id: state.review.request_id, confirmed: state.confirmed });
        if (!props.resetOnly) await history();
        state.review = null; emit("saved");
        if (props.resetOnly && state.result.ok && !state.result.warning) {
          msg.success("上游额度已重置"); emit("close");
        }
      });
      const closeReview = () => {
        if (state.busy) return;
        state.review = null; state.confirmed = false;
        if (props.resetOnly) emit("close");
      };
      const referrals = () => run("referral", async () => { state.referral = await api("GET", path() + "/referrals"); });
      const spark = () => run("spark", async () => { await api("POST", path() + "/spark", { models: state.spark, name: state.sparkName || null }); msg.success("Spark 影子账号已创建"); state.spark = []; emit("saved"); });
      const acknowledge = id => run("ack", async () => { await api("POST", path() + "/gpt-actions/acknowledge", { request_id: id, confirmed: true }); state.ack = null; await history(); });
      return { state, preview, execute, closeReview, referrals, spark, acknowledge, clock: absTime,
        actionLabel: a => a === "reset" ? "重置上游额度" : "发送邀请", stateLabel: s => ({prepared:'待确认',running:'执行中',done:'完成',failed:'失败',unknown:'结果待核对',acknowledged:'已人工核对'}[s] || s) };
    },
    template: `<div class="pool-gpt-operations">
      <template v-if="!resetOnly">
      <n-alert v-if="state.error" type="error" :show-icon="false">{{state.error}}</n-alert>
      <div v-if="row.parent_account_id" class="pool-detail-empty"><Ic name="link" :size="24"/><p>Spark 影子账号 · 主账号 #{{row.parent_account_id}}</p><p>凭据、代理、分组与容量继承主账号；额度和调用统计独立记录。重置券与邀请请到主账号操作。</p></div>
      <template v-else>
        <section class="pool-gpt-action-card"><div class="pool-section-heading"><h3><Ic name="gift" :size="17"/>上游额度重置</h3><n-tag size="small" :type="row.gpt_settings?.auto_reset?'warning':'default'">自动重置{{row.gpt_settings?.auto_reset?'已开启':'关闭'}}</n-tag></div>
          <p>可用 <b>{{row.gpt_reset_credits?.available_count ?? '—'}}</b> 张重置券。执行前会重新核对资格，成功后刷新额度并恢复上游限流状态。</p>
          <n-button secondary :loading="state.busy==='preview'" :disabled="!!state.busy" @click="preview('reset')">预览使用 1 张重置券</n-button>
          <p v-if="row.gpt_auto_reset_state" class="pool-field-hint">最近自动检查：{{clock(row.gpt_auto_reset_state.checked_at)}} · {{row.gpt_auto_reset_state.error || ({watching:'监测中',done:'已重置',unknown:'结果待核对',failed:'执行失败'}[row.gpt_auto_reset_state.state]) || '已检查'}}</p>
        </section>
        <section class="pool-gpt-action-card"><div class="pool-section-heading"><h3><Ic name="user" :size="17"/>Codex 邀请</h3><n-button text :loading="state.busy==='referral'" @click="referrals">读取资格</n-button></div>
          <p v-if="state.referral">{{state.referral.should_show && state.referral.available_invites?'可邀请':'当前不可邀请'}} · 剩余 {{state.referral.available_invites ?? '未知'}} 个名额</p><p v-if="state.referral?.description" class="pool-field-hint">{{state.referral.description}}</p>
          <div class="pool-form-grid"><n-input v-model:value="state.email" placeholder="收件人邮箱" :input-props="{autocomplete:'off'}"/><n-button secondary :disabled="!state.consent || !!state.busy || !state.email" @click="preview('referral')">预览邀请</n-button></div>
          <n-checkbox v-model:checked="state.consent" style="margin-top:12px">我已取得收件人同意，确认向此邮箱发送邀请</n-checkbox>
        </section>
        <section class="pool-gpt-action-card"><div class="pool-section-heading"><h3><Ic name="grid" :size="17"/>Spark 影子账号</h3></div><p>同一主账号最多一个影子账号。填写此上游实际支持的 Spark 模型 ID。</p>
          <n-dynamic-tags v-model:value="state.spark"/><n-space style="margin-top:12px"><n-input v-model:value="state.sparkName" placeholder="显示名称（可选）"/><n-button secondary :disabled="!state.spark.length || !!state.busy" :loading="state.busy==='spark'" @click="spark">创建 Spark</n-button></n-space>
        </section>
      </template>
      <section class="pool-gpt-action-card"><div class="pool-section-heading"><h3><Ic name="clock" :size="17"/>上游操作记录</h3></div><div v-if="!state.history.length" class="pool-field-hint">暂无重置券或邀请操作</div>
        <div v-for="item in state.history" :key="item.request_id" class="pool-gpt-history"><div><b>{{actionLabel(item.action)}}</b><span>{{item.email || (item.automatic?'自动执行':'手动执行')}}</span><small>{{clock(item.created_at)}}</small></div><div><n-tag :type="item.state==='done'?'success':['unknown','running'].includes(item.state)?'warning':'default'" size="small">{{stateLabel(item.state)}}</n-tag><p v-if="item.result?.error || item.result?.warning">{{item.result.error || item.result.warning}}</p><n-button v-if="['unknown','running'].includes(item.state)" text size="small" @click="state.ack=item.request_id">核对结果</n-button></div></div>
      </section>
      </template>
      <n-modal :show="resetOnly || !!state.review" preset="card" title="确认上游操作" class="pool-editor-modal" :mask-closable="!state.busy" :closable="!state.busy" :close-on-esc="!state.busy" @update:show="v=>{if(!v)closeReview()}">
        <n-alert v-if="state.error" type="error" :show-icon="false">{{state.error}}</n-alert>
        <template v-if="state.review"><n-alert type="warning" :show-icon="false">{{state.review.action==='reset'?'此操作将消耗 1 张真实上游重置券。':'此操作会向以下邮箱发送真实邀请：'+state.review.email}}</n-alert><p>账号 {{row.name}} · #{{row.id}}</p><n-checkbox v-model:checked="state.confirmed">确认执行{{state.review.action==='reset'?'并消耗 1 张券':'并向此邮箱发送邀请'}}</n-checkbox><n-space justify="end" style="margin-top:20px"><n-button :disabled="!!state.busy" @click="closeReview">取消</n-button><n-button type="primary" :disabled="!state.confirmed || !!state.busy" :loading="state.busy==='execute'" @click="execute">确认执行</n-button></n-space></template>
        <template v-else><p>账号 {{row.name}} · #{{row.id}}</p><n-spin v-if="state.busy==='preview'" size="small" description="正在核对重置券…"/>
          <n-alert v-if="state.result" :type="state.result.ok?'warning':'error'" :show-icon="false">{{state.result.warning || state.result.error || '请在上游核对结果'}}</n-alert>
          <n-space justify="end" style="margin-top:20px"><n-button :disabled="!!state.busy" @click="closeReview">关闭</n-button></n-space>
        </template>
      </n-modal>
      <n-alert v-if="!resetOnly && state.result" :type="state.result.ok?'success':'warning'" :show-icon="false">{{state.result.warning || state.result.error || (state.result.ok?'操作已完成':'请在上游核对结果')}}</n-alert>
      <n-modal :show="!!state.ack" preset="dialog" title="核对上游结果" positive-text="已在 Codex 核对" negative-text="取消" @positive-click="acknowledge(state.ack)" @negative-click="state.ack=null" @close="state.ack=null">请先在 Codex 确认券是否已消耗、邀请是否已发送，再解除待核对状态，避免重复操作。</n-modal>
    </div>`
  };
  const PAT = {
    props: ["show", "proxies"], emits: ["update:show", "saved"],
    setup(props, { emit }) {
      const state = reactive({ token: "", proxy_id: null, identity: null, busy: false, error: "" });
      watch(() => props.show, v => { if (v) Object.assign(state, { token: "", identity: null, error: "" }); });
      const send = async confirmed => { state.busy = true; state.error = ""; try { const data = await api("POST", root + "/pat/gpt", { token: state.token, proxy_id: state.proxy_id, confirmed }); if (confirmed) { state.token = ""; emit("update:show", false); emit("saved"); msg.success("PAT 账号已导入"); } else state.identity = data.identity; } catch (e) { state.error = e.message; } finally { state.busy = false; } };
      return { state, send };
    },
    template: `<n-modal :show="show" @update:show="v=>$emit('update:show',v)" preset="card" title="添加 Codex PAT" class="pool-editor-modal" :mask-closable="!state.busy" :closable="!state.busy"><n-alert v-if="state.error" type="error" :show-icon="false">{{state.error}}</n-alert><p>在线校验 at- 令牌的邮箱、工作区和套餐，再确认导入。PAT 不使用 OAuth 刷新令牌。</p><n-input v-model:value="state.token" type="password" show-password-on="click" placeholder="at-…" :input-props="{autocomplete:'off'}" @update:value="state.identity=null"/><n-select v-model:value="state.proxy_id" clearable :options="proxies" placeholder="直连" style="margin:16px 0" @update:value="state.identity=null"/><n-button secondary :loading="state.busy" @click="send(false)">在线校验</n-button><div v-if="state.identity" class="pool-gpt-action-card"><b>{{state.identity.email}}</b><p>{{state.identity.account_id}} · {{state.identity.plan_type}}</p><n-button type="primary" :loading="state.busy" @click="send(true)">确认导入此账号</n-button></div></n-modal>`
  };
  window.BitPortalGPT = { Settings, Operations, PAT };
})();
