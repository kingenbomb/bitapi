# bit-api —— 单容器、单 worker、SQLite 落在 /data 卷里(库、备份、价目表都在那)。
#
#   docker build -t bit-api .
#   docker run -d --name bit-api --env-file .env -p 127.0.0.1:8080:8080 -v ./data:/data bit-api
#
# 默认密钥不能上公网:容器里监听 0.0.0.0,不改 BITAPI_JWT_SECRET / API_KEY / ADMIN_KEY
# 进程会拒绝启动(core/preflight.py)。把 .env.example 抄成 .env 填好再起。
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TIKTOKEN_CACHE_DIR=/app/.tiktoken \
    BITAPI_HOST=0.0.0.0 \
    BITAPI_PORT=8080 \
    BITAPI_DB=/data/bitapi.db \
    BITAPI_PRICING_PATH=/data/litellm_pricing.json

WORKDIR /app

# 依赖单独一层:改代码不重装
COPY requirements.txt .
RUN pip install -r requirements.txt

# 估算 token 用的编码表在构建时拉好:线上容器往往不出网,拉不到会退化成「字符数/4」粗估
RUN python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"

COPY . .

# 不以 root 跑;/data 是唯一可写的地方
RUN useradd --system --uid 10001 --home /app bitapi \
    && mkdir -p /data && chown -R bitapi:bitapi /app /data
USER bitapi
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status == 200 else 1)"

CMD ["python", "main.py"]
