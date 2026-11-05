#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模型状态巡检 —— 每小时把在营渠道的模型各实测一遍,追加式落 model_status 历史。

口径(与网关/管理台测试同一套,不另发明):
  - 对话模型:优先流式探测拿 TTFT(首字延迟);流式失败原地回退非流式 —— 能答
    就算模型活着(无 TTFT)。失败把原话存进 error,并按网关同一套 mark_failure
    让坏号退场。失败换一把号再试一次(单 key 被上游单独限制会让整渠道误红)。
  - 向量模型:发一次最小 embeddings,成本几毫厘,无 TTFT。
  - 生成类(出图/出视频):**完全不巡检** —— 一次生成烧的是真金 credits,状态
    由号池巡检与渠道面板负责。
  - 结构化预测(kumo)等特殊形状:不产生记录,界面上是「未检测」。
  - 已下线渠道不巡检(enabled_adapters 口径)。

记录保留 7 天,每轮巡检顺手清理过期行。状态条 = 最近 7 天按天聚合:
  渠道视角(管理台)取最差 —— 当天任一次失败就是红;
  用户视角(控制台)取最好 —— 当天任一渠道能用就是绿(多渠道故障切换)。
  绿=正常 红=失败 黄=号池空(暂不可用) 灰=当天没测。

状态翻转时 emit 事实,告警插件自行决定要不要通知:
  model.failed     上次通过 → 这次失败/号池空
  model.recovered  上次失败 → 这次通过

参数约定:db = UserDB(落 model_status 行);pool = core.pool.Pool(取号/罚号);
pool_db = core.db.DB(生成类推定用;本模块现在不实测生成类,保留给兜底分支)。
"""
import asyncio
import re
import time

import config
from core.hooks import emit
from core.account_runtime import network

OK, FAIL, SKIP = 1, 0, -1
# 状态条颜色档(grade):1=绿 0=红 2=黄;None=没测(灰)
BAR_GREEN, BAR_RED, BAR_YELLOW = 1, 0, 2
RETENTION = 7 * 86400
_CONCURRENCY = 4


def monitored_models(adapter):
    # One GPT account shares its capacity across model names; use Terra as the
    # requested monitoring target instead of probing every callable GPT model.
    if adapter.name == "gpt":
        return ["gpt-6.1-terra"]
    from core.openai_images import supports
    return [model for model in adapter.models if not supports(adapter, model)]

# 给普通用户看的失败原因:抹掉凭证与上游地址,只留可读短句
_SENSITIVE = (
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}"),
    re.compile(r"(?i)\b(sk|pk|rk|ak|ghp|gho|ghs|xox[baprs])[-_][A-Za-z0-9._-]{6,}"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|cookie)\s*[=:]\s*\S+"),
    re.compile(r"https?://[^\s\"'<>)]+"),
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"),
)


def public_error(text, limit=90):
    """把上游报错洗成可以公开给用户的一行:去掉 key/token/URL/内网地址。

    保留 "HTTP 429 Too Many Requests"、"401 Unauthorized" 这类可操作信息,
    抹掉 "Bearer xxx"、"https://upstream.internal/..." 这类不该出网关的部分。
    空白折叠成单空格,超长截断加省略号。"""
    if not text:
        return None
    out = str(text)
    for pat in _SENSITIVE:
        out = pat.sub("[已隐去]", out)
    out = re.sub(r"\s+", " ", out).strip()
    if not out:
        return None
    if len(out) > limit:
        out = out[:limit].rstrip() + "…"
    return out

# 巡检用的探针话术,与管理台「测试渠道」一致
PROMPT = "Reply with the single word: pong"


def _probe_body():
    return {"max_tokens": 32, "temperature": 0,
            "messages": [{"role": "user", "content": PROMPT}]}


def _streamable(ad):
    return bool(getattr(ad, "streaming", False) and hasattr(ad, "stream_chat"))


def _chat_call(ad, acct, model, stream):
    """发一次探测对话。返回 (ttft_ms, total_ms);失败抛异常。

    ttft = 第一条 SSE 数据事件(任何 token,含 reasoning)距发起的毫秒数 ——
    思考型模型先出 reasoning,对用户那就是首字。"""
    started = time.time()
    ttft = None
    if stream and _streamable(ad):
        def on_sse(line):
            nonlocal ttft
            if ttft is None and line.startswith("data:") and "[DONE]" not in line:
                ttft = int((time.time() - started) * 1000)
        ad.stream_chat(acct, model, _probe_body(), on_sse)
        return (ttft if ttft is not None else None,
                int((time.time() - started) * 1000))
    reply = ad.chat(acct, _probe_body()["messages"], stream=False, model=model,
                    body=_probe_body())
    raw = (reply or {}).get("raw") if isinstance(reply, dict) else None
    if isinstance(reply, dict) and not raw and not (reply.get("content") or ""):
        # 自产响应的渠道(chat 返回 {content,...})没出字也算失败;
        # raw 型(OpenAI 兼容)不看 content —— 非流式下思考型模型的 content
        # 在 reasoning 后面,32 token 拿到 finish 即算通。
        raise RuntimeError("上游返回空内容")
    return None, int((time.time() - started) * 1000)


def _probe_chat(ad, pool, model, pool_db=None):
    """对话探测:流式优先(拿 TTFT),失败原地回退非流式;换号重试一次。

    返回 (ok, latency_ms, ttft_ms, error)。"""
    error = None
    tried = set()
    started = time.time()
    pool_db = pool_db if pool_db is not None else getattr(pool, "db", None)
    for attempt in (1, 2):
        acct = pool.get_valid_account(ad.name, ad, **({"exclude": tried} if ad.name == "gpt" else {}))
        if acct is None:
            return (FAIL if error else SKIP), int((time.time() - started) * 1000), None, error or "号池里没有可用账号"
        if ad.name == "gpt" and acct["id"] in tried:
            return FAIL, int((time.time() - started) * 1000), None, error
        tried.add(acct["id"])
        stream = _streamable(ad)
        try:
            with network(pool_db, acct):
                try:
                    ttft, _ = _chat_call(ad, acct, model, stream)
                except Exception as exc:
                    skip_fallback = not stream
                    if ad.name == "gpt":
                        from core.gpt_request_policy import can_switch, status_code
                        skip_fallback = skip_fallback or not can_switch(exc) or status_code(exc) == 429 or getattr(exc, "_gpt_capacity_retries_exhausted", False)
                    if skip_fallback:
                        raise
                    # The fallback is part of the same health check. A healthy
                    # nonstream response must not leave the account in cooldown.
                    ttft, _ = _chat_call(ad, acct, model, stream=False)
            if ad.name == "gpt" and pool_db is not None:
                pool_db.update_account(acct["id"], last_error="")
            return OK, int((time.time() - started) * 1000), ttft, None
        except Exception as e:
            # Decide account health only after both transports have failed.
            pool.mark_failure(acct["id"], e, adapter=ad)
            error = str(e)[:500]
            if ad.name == "gpt" and pool_db is not None:
                from core.account_management import safe_error
                error = safe_error(e, acct)
                pool_db.update_account(acct["id"], last_error=error)
            if ad.name == "gpt":
                from core.gpt_request_policy import can_switch
                if not can_switch(e, len(tried)):
                    return FAIL, int((time.time() - started) * 1000), None, error
    return FAIL, int((time.time() - started) * 1000), None, error


def _probe_embed(ad, pool, model):
    """向量探测。返回 (ok, latency_ms, ttft_ms, error)。失败换一把号再试一次。"""
    error, latency = None, 0
    for attempt in (1, 2):
        acct = pool.get_valid_account(ad.name, ad)
        if acct is None:
            return SKIP, latency, None, "号池里没有可用账号"
        started = time.time()
        try:
            ad.embeddings(acct, {"model": model, "input": ["ping"]})
        except Exception as e:
            pool.mark_failure(acct["id"], e, adapter=ad)
            error = str(e)[:500]
            latency += int((time.time() - started) * 1000)
            if attempt == 1:
                continue
            return FAIL, latency, None, error
        return OK, latency + int((time.time() - started) * 1000), None, None
    return FAIL, latency, None, error or "unknown"


def probe_one(ad, pool, pool_db, model):
    """测一个模型。返回 (ok, latency_ms, ttft_ms, error) 或 None(不产生记录)。"""
    if model in (getattr(ad, "prediction_models", None) or []):
        return None    # 结构化预测不走对话协议,不实测
    if getattr(ad, "proxy", False):
        # 转发型没有本地号池路径;有 proxy_chat 就透传一句(无 TTFT 口径)
        if hasattr(ad, "proxy_chat"):
            started = time.time()
            try:
                ad.proxy_chat({"model": model, "max_tokens": 32,
                               "messages": [{"role": "user", "content": PROMPT}]},
                              stream=False)
            except Exception as e:
                return FAIL, int((time.time() - started) * 1000), None, str(e)[:500]
            return OK, int((time.time() - started) * 1000), None, None
        return None
    if getattr(ad, "kind", "chat") == "generation":
        return None    # 生成类(图/视频)不巡检:一次生成烧真金 credits
    if ad.has("chat"):
        if not ad.supports_chat(model):
            return None
        return _probe_chat(ad, pool, model, pool_db)
    if hasattr(ad, "embeddings"):
        return _probe_embed(ad, pool, model)
    return None


def _record(db, ad, model, result):
    """落历史 + 状态翻转时发事件。result=(ok, latency_ms, ttft_ms, error)。"""
    ok, ms, ttft, err = result
    prev = db.get_model_status(ad.name, model)
    db.add_model_status(ad.name, model, ok, latency_ms=ms, ttft_ms=ttft, error=err)
    if prev is not None:
        was = int(prev.get("ok") or 0)
        if was == OK and ok != OK:
            emit("model.failed", channel=ad.name, model=model, error=err)
        elif was != OK and ok == OK:
            emit("model.recovered", channel=ad.name, model=model)


async def check_all(db, pool, pool_db, adapters, log=print):
    """巡检一轮在营渠道的全部可实测模型。adapters: {name: adapter}。
    返回 {ok, fail, skip}。生成类渠道整体跳过。"""
    sem = asyncio.Semaphore(_CONCURRENCY)
    tally = {"ok": 0, "fail": 0, "skip": 0}
    targets = []
    for name, ad in sorted(adapters.items()):
        if getattr(ad, "kind", "chat") == "generation":
            continue
        for model in sorted(monitored_models(ad)):
            targets.append((ad, model))

    async def guarded(ad, model):
        async with sem:
            result = await asyncio.to_thread(probe_one, ad, pool, pool_db, model)
            if result is None:
                return
            await asyncio.to_thread(_record, db, ad, model, result)
            tally["ok" if result[0] == OK else ("fail" if result[0] == FAIL else "skip")] += 1

    if targets:
        t0 = time.time()
        await asyncio.gather(*(guarded(ad, m) for ad, m in targets))
        log("[model-status] 检测 %d 个模型 %.1fs -> ok=%d fail=%d skip=%d"
            % (len(targets), time.time() - t0, tally["ok"], tally["fail"], tally["skip"]))
    return tally


def grade_of(ok, ttft_ms=None, latency_ms=0):
    """单次检测的颜色档。

    红(0)=不能用:实测失败,或号池空(用户调用就是 503);
    黄(2)=能用但慢:通过,且首字(无首字按总耗时)超过 MODEL_STATUS_SLOW_MS;
    绿(1)=正常。"""
    if ok != OK:          # FAIL 与 SKIP(号池空)对用户都是「不能用」
        return BAR_RED
    ref = ttft_ms if ttft_ms else (latency_ms or 0)
    return BAR_YELLOW if ref > config.MODEL_STATUS_SLOW_MS else BAR_GREEN


def hour_bars(rows, hours=24, now=None, worst=True):
    """把检测行按小时聚合为最近 N 个心跳槽。返回 [{slot, label, grade}] 从旧到新。

    巡检每小时一轮,所以一个槽就是一次心跳。slot 是槽起点 epoch 秒,
    label 是给人看的 "HH:00"(跨天时带日期)。
    grade: 1=绿 0=红(不能用) 2=黄(通过但慢) None=该小时没测。
    worst=True(渠道视角):该小时出现过红就是红,否则出现过黄就是黄;
    worst=False(用户视角):该小时任一渠道绿就是绿,否则任一黄就是黄
    (多渠道故障切换,能用就行)。"""
    now = now or time.time()
    slot_seconds = 3600
    current = int(now // slot_seconds) * slot_seconds
    buckets = {}
    for r in rows:
        checked = r.get("checked_at") or 0
        slot = int(checked // slot_seconds) * slot_seconds
        buckets.setdefault(slot, []).append(
            grade_of(r.get("ok"), r.get("ttft_ms"), r.get("latency_ms")))
    out = []
    for i in range(hours - 1, -1, -1):
        slot = current - i * slot_seconds
        grades = buckets.get(slot)
        stamp = time.localtime(slot)
        same_day = time.localtime(current)[:3] == stamp[:3]
        label = time.strftime("%H:00", stamp) if same_day else time.strftime("%m-%d %H:%M", stamp)
        if not grades:
            grade = None
        elif worst:
            grade = (BAR_RED if BAR_RED in grades
                     else (BAR_YELLOW if BAR_YELLOW in grades else BAR_GREEN))
        else:
            grade = (BAR_GREEN if BAR_GREEN in grades
                     else (BAR_YELLOW if BAR_YELLOW in grades else BAR_RED))
        out.append({"slot": slot, "label": label, "grade": grade})
    return out


def day_bars(rows, days=7, now=None, worst=True):
    """把检测行按天聚合为最近 N 天的状态条。返回 [{date, grade}] 从旧到新。

    grade: 1=绿 0=红(不能用) 2=黄(通过但慢) None=当天没测。
    worst=True(渠道视角):当天出现过红就是红,否则出现过黄就是黄;
    worst=False(用户视角):当天任一渠道绿就是绿,否则任一黄就是黄
    (多渠道故障切换,能用就行)。"""
    now = now or time.time()
    buckets = {}
    for r in rows:
        day = time.strftime("%m-%d", time.localtime(r["checked_at"]))
        buckets.setdefault(day, []).append(
            grade_of(r.get("ok"), r.get("ttft_ms"), r.get("latency_ms")))
    out = []
    for i in range(days - 1, -1, -1):
        day = time.strftime("%m-%d", time.localtime(now - i * 86400))
        grades = buckets.get(day)
        if not grades:
            out.append({"date": day, "grade": None})
        elif worst:
            grade = (BAR_RED if BAR_RED in grades
                     else (BAR_YELLOW if BAR_YELLOW in grades else BAR_GREEN))
            out.append({"date": day, "grade": grade})
        else:
            grade = (BAR_GREEN if BAR_GREEN in grades
                     else (BAR_YELLOW if BAR_YELLOW in grades else BAR_RED))
            out.append({"date": day, "grade": grade})
    return out


def user_status_map(adapters, stored, hist_by_key, days=7, now=None, with_channels=False):
    """用户视角的模型状态总表,给控制台状态页与模型广场共用。

    返回 {model: {ok, latency_ms, ttft_ms, checked_at, heartbeats}}:
      ok 1=任一渠道实测通过 0=全部实测失败 -1=号池空 None=未检测;
      heartbeats = 最近 24 次心跳(每小时槽,多渠道任一生效即绿);
      bars = 兼容字段,保留 7 天按天状态条。
    with_channels=True 时额外带 channels(承载该模型的在营渠道名),
    只给登录后的状态页用;模型广场是游客可见的,渠道信息照例不出网关。
    error 是**洗过**的失败原因(public_error 抹掉 key/token/URL/内网地址),
    只在该模型整体失败时有值,给状态页的「失败原因」列用。
    生成类(图/视频)不巡检,不在结果里。"""
    per = {}
    serving = {}
    kinds = {}
    for name, ad in sorted(adapters.items()):
        if getattr(ad, "kind", "chat") == "generation":
            continue
        for model in monitored_models(ad):
            serving.setdefault(model, []).append((name, model))
            kinds.setdefault(model, set()).add(getattr(ad, "kind", "chat"))
            r = stored.get((name, model))
            if r is not None:
                per.setdefault(model, []).append(r)
    out = {}
    for model, keys in serving.items():
        rows = per.get(model) or []
        if rows:
            oks = [r for r in rows if int(r.get("ok") or 0) == OK]
            fails = [r for r in rows if int(r.get("ok") or 0) == FAIL]
            if oks:
                ok, lat = OK, min((r.get("latency_ms") or 0) for r in oks)
                ttfts = [r.get("ttft_ms") for r in oks if r.get("ttft_ms")]
                ttft = min(ttfts) if ttfts else None
            elif fails:
                ok, lat, ttft = FAIL, max((r.get("latency_ms") or 0) for r in fails), None
            else:
                ok, lat, ttft = SKIP, 0, None
            checked = max((r.get("checked_at") or 0) for r in rows) or None
        else:
            ok = lat = checked = None
            ttft = None
        combined = [r for key in keys for r in hist_by_key.get(key, [])]
        # 失败原因:多渠道时优先取最后一次失败的那条,洗掉凭证与上游地址
        err = None
        if ok in (FAIL, SKIP):
            cand = sorted(rows, key=lambda r: r.get("checked_at") or 0, reverse=True)
            for r in cand:
                err = public_error(r.get("error"))
                if err:
                    break
        entry = {"ok": ok, "latency_ms": lat or 0, "ttft_ms": ttft,
                 "checked_at": checked,
                 "kind": "embed" if kinds.get(model) == {"embed"} else "chat",
                 "heartbeats": hour_bars(combined, hours=24, now=now, worst=False),
                 "bars": day_bars(combined, days=days, now=now, worst=False)}
        if with_channels:
            # 渠道名与失败原因都算「网关内情」,只给登录后的状态页;
            # 模型广场(游客可见)照旧只出 ok/延迟/心跳。
            entry["channels"] = sorted({name for name, _ in keys})
            entry["error"] = err
        out[model] = entry
    return out


def aggregate_for_user(adapters, stored):
    """用户视角的模型状态:按对外模型名聚合,多渠道取最优。

    任一渠道实测通过 = 正常(ok=1);否则任一实测失败 = 故障(0);
    否则有号池空 = 暂不可用(-1);完全没查过 = None(未检测)。
    故障与暂不可用排最前 —— 用户打开这页是想看「现在什么不能用」。

    只回对外模型名,不带渠道名与上游错误原话:渠道信息不出网关。
    返回 [{model, ok, latency_ms, ttft_ms, checked_at}]。"""
    per = {}
    for name, ad in sorted(adapters.items()):
        if getattr(ad, "kind", "chat") == "generation":
            continue
        for model in monitored_models(ad):
            r = stored.get((name, model))
            if r is not None:
                per.setdefault(model, []).append(r)
    out = []
    for model in sorted({m for _, ad in adapters.items()
                         if getattr(ad, "kind", "chat") != "generation"
                         for m in monitored_models(ad)}):
        rows = per.get(model) or []
        if not rows:
            out.append({"model": model, "ok": None, "latency_ms": 0,
                        "ttft_ms": None, "checked_at": None})
            continue
        oks = [r for r in rows if int(r.get("ok") or 0) == OK]
        fails = [r for r in rows if int(r.get("ok") or 0) == FAIL]
        if oks:
            ok, lat = OK, min((r.get("latency_ms") or 0) for r in oks)
            ttfts = [r.get("ttft_ms") for r in oks if r.get("ttft_ms")]
            ttft = min(ttfts) if ttfts else None
        elif fails:
            ok, lat = FAIL, max((r.get("latency_ms") or 0) for r in fails)
            ttft = None
        else:
            ok, lat, ttft = SKIP, 0, None
        checked = max((r.get("checked_at") or 0) for r in rows) or None
        out.append({"model": model, "ok": ok, "latency_ms": lat,
                    "ttft_ms": ttft, "checked_at": checked})
    order = {FAIL: 0, SKIP: 1, None: 2, OK: 3}
    out.sort(key=lambda r: (order.get(r["ok"], 2), r["model"]))
    return out


# 手动触发与定时轮共用同一道闸:同时只跑一轮,避免探针流量翻倍
_running = False


def is_running():
    return _running


async def run_checked(db, pool, pool_db, adapters_fn, log=print):
    """跑一轮(有人在跑就跳过)。返回是否真的跑了。顺带清理过期历史。"""
    global _running
    if _running:
        return False
    _running = True
    try:
        await check_all(db, pool, pool_db, adapters_fn(), log=log)
        await asyncio.to_thread(db.prune_model_status, time.time() - RETENTION)
        return True
    finally:
        _running = False


async def loop(db, pool, pool_db, adapters_fn, interval, log=print, first_delay=90):
    """后台常驻:启动先等 first_delay(避开冷启动争抢),之后每 interval 秒一轮。"""
    await asyncio.sleep(first_delay)
    while True:
        try:
            ran = await run_checked(db, pool, pool_db, adapters_fn, log=log)
            if not ran:
                log("[model-status] 上一轮(手动触发)仍在跑,本轮跳过")
        except Exception as e:
            log(f"[model-status] error: {e}")
        await asyncio.sleep(interval)
