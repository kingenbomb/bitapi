# 部署

三种起法,同一份代码。前置只有一条:**三个密钥必须改**(`BITAPI_JWT_SECRET` / `BITAPI_API_KEY` / `BITAPI_ADMIN_KEY`)。它们都有能用的默认值,本地起一下方便,但监听到非回环地址还用默认值等于没有密码 —— 进程会拒绝启动(`core/preflight.py`),不再只是 README 里一句提醒。确实要在内网裸跑,设 `BITAPI_ALLOW_DEFAULT_SECRETS=1` 明确写下来。

环境变量样板在 [`.env.example`](../.env.example),抄成 `.env` 填好。`.env` 在 `.gitignore` 里,永远不进仓。

## 一、Docker(推荐给第一次部署的人)

```bash
cp .env.example .env        # 填密钥、站点地址、SMTP、告警地址
docker compose up -d
docker compose logs -f      # 看到「Application startup complete」即好
```

- 容器里监听 `0.0.0.0:8080`,compose 只把它映射到宿主机 `127.0.0.1:8080`,对外由 nginx 做 TLS(见下)
- `./data` 挂到容器 `/data`:库(`bitapi.db`)、备份(`backups/`)、价目表都在这一个目录里。**备份它就是备份全部**
- 镜像构建时把 tiktoken 的编码表拉好了,线上容器不出网也能精确估算 token
- 单 worker 是前提(限流、渠道开关、告警去重都是进程内状态),不要 scale

升级:`git pull && docker compose up -d --build`。库结构变更在启动时自动迁移(`UserDB._migrate`),不用手工跑脚本。

## 二、systemd(裸机)

```bash
useradd --system --home /opt/bit-api bitapi
git clone <repo> /opt/bit-api && cd /opt/bit-api
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && $EDITOR .env
cp deploy/bit-api.service /etc/systemd/system/
chown -R bitapi:bitapi /opt/bit-api
systemctl enable --now bit-api
journalctl -u bit-api -f
```

单元文件 [`deploy/bit-api.service`](../deploy/bit-api.service) 里几处不是装饰:

- `LimitNOFILE=65536`:2026-09-05 的事故是 fd 撞 1024 把站点打死(每条流式请求两个 fd 起步),见 `docs/incidents/`
- `Environment=BITAPI_HOST=127.0.0.1`:生产只监听回环,对外靠 nginx
- `ProtectSystem=strict` + `ReadWritePaths=/opt/bit-api`:进程只能写自己的目录

## 三、直接跑(开发)

```bash
pip install -r requirements.txt
python main.py                 # 默认 0.0.0.0:8080;密钥是默认值时会拒绝 —— 加 BITAPI_HOST=127.0.0.1 或改密钥
```

## nginx

样板在 [`deploy/nginx.conf.example`](../deploy/nginx.conf.example)。四处漏了会出具体的事故:`proxy_buffering off`(不然流式变成等完一次吐)、`proxy_read_timeout 600s`(生图与长思考几分钟,默认 60 秒会掐)、`X-Forwarded-For`(限速与密钥 IP 白名单读它,不传全站共享一个桶)、`client_max_body_size`(带图请求几 MB,默认 1m 会 413)。

`/pay/notify/` 必须能从公网打进来;`/admin/*` 号池端点现在认管理密钥也认管理员 JWT,管理台自己就能操作号池,不再需要 nginx 层注入密钥。

## 上线前核对

- [ ] 三个密钥已改;`BITAPI_SITE_URL` 是公网域名(支付回调、找回密码链接都用它)
- [ ] 管理台「站点设置」:注册是否要邀请码、默认分组、SMTP(点「发测试邮件」)、支付渠道
- [ ] 管理台「渠道」:上游地址、导 key、逐个「测试」;「模型定价」给每个模型配价(没配价按免费计)
- [ ] `BITAPI_PLUGINS` 含 `alert_webhook` 且 `BITAPI_ALERT_WEBHOOK_URL` 已设 —— 号池打空、订单卡住、备份失败、SMTP 坏了都靠它通知
- [ ] 「运行状态」里看到第一份备份已生成

## 恢复

停服务 → 把 `backups/bitapi-YYYYmmdd-HHMMSS.db` 复制成库文件的路径(先把现有的连同 `-wal` / `-shm` 挪走)→ 启动。备份是普通 SQLite 文件,`sqlite3 backups/xxx.db "select count(*) from users"` 就能验。详见 [operations.md](operations.md)。
