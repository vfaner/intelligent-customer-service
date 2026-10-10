# 部署指南

从本机跑通到部署上线的完整步骤。

**目录**

- [部署前准备](#部署前准备)
- [方案 A：Docker Compose（推荐）](#方案-adocker-compose推荐)
- [方案 B：systemd + Nginx](#方案-bsystemd--nginx)
- [Nginx 配置（含 SSE 关键设置）](#nginx-配置含-sse-关键设置)
- [HTTPS 证书](#https-证书)
- [数据库选型：SQLite、MySQL、PostgreSQL 与信创库](#数据库选型sqlitemysqlpostgresql-与信创库)
- [向量库选型与部署](#向量库选型与部署)
- [首次上线检查清单](#首次上线检查清单)
- [安全加固](#安全加固)
- [备份与恢复](#备份与恢复)
- [升级与回滚](#升级与回滚)
- [监控与日志](#监控与日志)
- [故障排查](#故障排查)

---

## 部署前准备

### 服务器配置建议

| 用途 | CPU | 内存 | 磁盘 | 说明 |
|---|---|---|---|---|
| 小规模（<1 万片段、Chroma） | 2 核 | 2 GB | 20 GB | 够用 |
| 中等（1~10 万片段、Chroma/Qdrant） | 2 核 | 4 GB | 40 GB | 推荐起点 |
| 大规模（>10 万片段、Qdrant/Milvus） | 4 核 | 8 GB+ | 100 GB+ | 向量库独立部署更好 |

> 向量数据比想象的占空间：本项目实测 **12,596 个片段（2048 维）的 Chroma 目录约 163 MB**。
> 按这个比例估算你的磁盘需求，并留出 2~3 倍余量给索引和备份。

LLM 推理在厂商侧，所以对本机 CPU/GPU 无要求。

### 需要准备的东西

- [ ] 一台 Linux 服务器（Ubuntu 22.04 / Debian 12 / CentOS Stream 9 均可）
- [ ] 一个域名并解析到服务器 IP（要用 HTTPS 就必须有）
- [ ] 对话模型的 API Key
- [ ] Embedding 模型的 API Key（可与上面同一个，取决于厂商）
- [ ] 服务器安全组/防火墙开放 80、443

### 端口规划

| 端口 | 用途 | 是否对外 |
|---|---|---|
| 80 / 443 | Nginx | ✅ 对外 |
| 8000 | 应用（uvicorn） | ❌ 只监听 127.0.0.1 |
| 3306 | MySQL / TiDB / OceanBase | ❌ 仅内网 |
| 5432 | PostgreSQL / openGauss / HighGo / Vastbase | ❌ 仅内网 |
| 54321 | 人大金仓 KingbaseES | ❌ 仅内网 |
| 5236 | 达梦 DM8 | ❌ 仅内网 |
| 6379 | Redis | ❌ 仅内网 |
| 6333 | Qdrant | ❌ 仅内网 |
| 19530 | Milvus | ❌ 仅内网 |

**只有 Nginx 该暴露在公网。** 管理后台自带登录，但应用直接绑 `0.0.0.0:8000` 对外，
就把登录页、以及数据库和向量库的端口一起摆到了公网上 —— 少一层是一层。

---

## 方案 A：Docker Compose（推荐）

适合大多数场景：环境隔离、一条命令起停、迁移方便。

### 1. 装 Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo systemctl enable --now docker
sudo usermod -aG docker $USER    # 重新登录生效
```

### 2. 拉代码

```bash
sudo mkdir -p /opt && cd /opt
sudo git clone https://github.com/vfaner/intelligent-customer-service.git intelligent-customer-service
sudo chown -R $USER:$USER intelligent-customer-service
cd intelligent-customer-service
```

### 3. 写应用的 Dockerfile

项目里没有内置 Dockerfile（本地开发不需要），新建 `backend/Dockerfile`：

```dockerfile
FROM python:3.12-slim

# tini 负责回收僵尸进程；curl 供 healthcheck 使用
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl tini && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖单独一层：改代码时不必重装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# 前端静态文件在上一层目录，main.py 按相对路径找它
COPY ../frontend /frontend

EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

> ⚠️ Dockerfile 里 `COPY ../frontend` 是非法的（不能超出构建上下文）。
> 所以下面的 compose 把**项目根目录**作为构建上下文，`dockerfile` 指向 `backend/Dockerfile`，
> 并把 `COPY` 路径改成相对项目根。修正版：

```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl tini && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ /app/
COPY frontend/ /frontend/

EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### 4. 写生产用的 compose 文件

项目自带的 `backend/docker-compose.yml` 只起依赖服务（MySQL/Redis/Qdrant/Milvus），
不含应用本身。新建项目根目录的 `docker-compose.prod.yml`：

```yaml
services:
  app:
    build:
      context: .                      # 项目根，才能同时 COPY backend/ 和 frontend/
      dockerfile: backend/Dockerfile
    restart: unless-stopped
    env_file: backend/.env
    ports:
      - "127.0.0.1:8000:8000"         # 只绑本机，由 Nginx 转发
    volumes:
      # 数据放宿主机：容器重建不丢配置、会话和向量数据
      - ./backend/data:/app/data
      - ./backend/uploads:/app/uploads
    depends_on:
      mysql:
        condition: service_healthy
      redis:
        condition: service_started
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:8000/health"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s

  mysql:
    image: mysql:8.0
    restart: unless-stopped
    environment:
      MYSQL_ROOT_PASSWORD: ${MYSQL_ROOT_PASSWORD:?请在 .env 里设置}
      MYSQL_DATABASE: cs_db
      MYSQL_USER: cs_user
      MYSQL_PASSWORD: ${MYSQL_PASSWORD:?请在 .env 里设置}
    command: --character-set-server=utf8mb4 --collation-server=utf8mb4_unicode_ci
    volumes:
      - mysql_data:/var/lib/mysql
    healthcheck:
      test: ["CMD", "mysqladmin", "ping", "-h", "127.0.0.1", "-p$$MYSQL_ROOT_PASSWORD"]
      interval: 10s
      retries: 10

  redis:
    image: redis:7-alpine
    restart: unless-stopped
    volumes:
      - redis_data:/data

  # 用 Qdrant 时取消注释
  # qdrant:
  #   image: qdrant/qdrant:latest
  #   restart: unless-stopped
  #   volumes:
  #     - qdrant_data:/qdrant/storage

volumes:
  mysql_data:
  redis_data:
  # qdrant_data:
```

> `${MYSQL_PASSWORD:?...}` 的写法会在变量缺失时**直接报错退出**，
> 比悄悄用空密码启动安全。

### 5. 配置环境变量

```bash
cp backend/.env.example backend/.env
nano backend/.env
```

生产环境至少要改这几项：

```env
APP_ENV=production
APP_DEBUG=false
# 后台登录令牌用它签名，务必换掉
APP_SECRET_KEY=<用下面的命令生成>

DATABASE_URL=mysql+aiomysql://cs_user:<你的密码>@mysql:3306/cs_db
REDIS_URL=redis://redis:6379/0
REDIS_ENABLED=true

# 只允许你自己的站点嵌入，不要留 *
CORS_ORIGINS=https://你的域名,https://www.你的域名

# compose 会读这两个变量去初始化 MySQL
MYSQL_ROOT_PASSWORD=<强密码>
MYSQL_PASSWORD=<强密码>
```

生成 SECRET_KEY：

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

> **API Key 不用写进 `.env`** —— 启动后在管理后台页面填即可，会存进数据库。
> 如果你更希望用环境变量管理（比如 CI/CD 场景），填 `LLM_API_KEY` / `EMBEDDING_API_KEY` 也生效。

**容器内的主机名**：`DATABASE_URL` 里要用 `mysql` 而不是 `localhost` ——
compose 里服务名就是主机名。这是最常见的踩坑点。

### 6. 启动

```bash
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml ps          # 状态应为 healthy
docker compose -f docker-compose.prod.yml logs -f app # 看启动日志
curl -fsS http://127.0.0.1:8000/health                # 应返回 {"status":"ok"}
```

常用运维命令：

```bash
docker compose -f docker-compose.prod.yml restart app   # 重启应用
docker compose -f docker-compose.prod.yml down          # 停止（数据保留）
docker compose -f docker-compose.prod.yml up -d --build # 更新代码后重建
docker compose -f docker-compose.prod.yml logs --tail=200 app
```

---

## 方案 B：systemd + Nginx

不想用 Docker、或想直接在宿主机调试时用这个。

### 1. 装 Python 与依赖

```bash
# Ubuntu / Debian
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3-pip nginx git

# CentOS / RHEL
sudo dnf install -y python3.12 python3-pip nginx git
```

### 2. 建专用用户（不要用 root 跑应用）

```bash
sudo useradd -r -s /bin/false -d /opt/ics csapp
sudo mkdir -p /opt/ics && cd /opt/ics
sudo git clone https://github.com/vfaner/intelligent-customer-service.git .
sudo chown -R csapp:csapp /opt/ics
```

### 3. 装依赖

```bash
cd /opt/ics/backend
sudo -u csapp python3 -m venv .venv
sudo -u csapp .venv/bin/pip install -r requirements.txt
sudo -u csapp cp .env.example .env
sudo nano .env        # 按上面「配置环境变量」改，DATABASE_URL 用 localhost
```

SQLite 用户注意目录权限：

```bash
sudo -u csapp mkdir -p /opt/ics/backend/data /opt/ics/backend/uploads
```

### 4. 写 systemd 服务

`/etc/systemd/system/ics.service`：

```ini
[Unit]
Description=Intelligent Customer Service
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=csapp
Group=csapp
WorkingDirectory=/opt/ics/backend
EnvironmentFile=/opt/ics/backend/.env
# 用 uvicorn 而非 run.py：生产不需要热重载和启动横幅
ExecStart=/opt/ics/backend/.venv/bin/uvicorn src.main:app \
          --host 127.0.0.1 --port 8000 --workers 2 --proxy-headers
Restart=always
RestartSec=5

# 收紧权限：只有数据目录可写
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/ics/backend/data /opt/ics/backend/uploads

StandardOutput=journal
StandardError=journal
SyslogIdentifier=ics

[Install]
WantedBy=multi-user.target
```

启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ics
sudo systemctl status ics
sudo journalctl -u ics -f        # 看日志
curl -fsS http://127.0.0.1:8000/health
```

### 关于 `--workers`

多进程能提高并发，但要注意：

| 向量库 | 多 worker 是否安全 |
|---|---|
| **Chroma**（本地文件） | ⚠️ 多进程写同一目录可能冲突，建议 `--workers 1` |
| **Qdrant / Milvus**（独立服务） | ✅ 可以开多个 |

用 Chroma 且需要更高并发时，改用 Qdrant，或保持单 worker（本项目瓶颈在
LLM 厂商响应，单 worker 通常够用）。

---

## Nginx 配置（含 SSE 关键设置）

`/etc/nginx/sites-available/ics`（CentOS 放 `/etc/nginx/conf.d/ics.conf`）：

```nginx
upstream ics_backend {
    server 127.0.0.1:8000;
    keepalive 32;
}

server {
    listen 80;
    server_name 你的域名;
    # 装完证书后由 certbot 自动改成 301 跳转
    location / { return 301 https://$host$request_uri; }
}

server {
    listen 443 ssl http2;
    server_name 你的域名;

    ssl_certificate     /etc/letsencrypt/live/你的域名/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/你的域名/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_session_cache shared:SSL:10m;

    # 知识文档最大 20 MB（与 APP_MAX_UPLOAD_MB 保持一致，留些余量）
    client_max_body_size 25M;

    access_log /var/log/nginx/ics.access.log;
    error_log  /var/log/nginx/ics.error.log;

    # ─── SSE 流式问答：必须关闭缓冲 ───────────────────
    # 不加这段，回答不会逐字出现，而是等全部生成完才一次性蹦出来，
    # 长回答还可能直接超时断开。这是部署本项目最容易踩的坑。
    location /api/chat/stream {
        proxy_pass http://ics_backend;
        proxy_http_version 1.1;
        proxy_set_header Connection '';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_buffering off;
        proxy_cache off;
        chunked_transfer_encoding off;
        proxy_read_timeout 300s;     # 长回答 + 429 重试可能耗时较久
    }

    # ─── 文档入库：耗时长，超时要放宽 ─────────────────
    # 大知识库入库一次可能几分钟（受 embedding 厂商配额影响）
    location /api/documents/process {
        proxy_pass http://ics_backend;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 1800s;
        proxy_send_timeout 1800s;
    }

    # ─── 其余请求 ─────────────────────────────────────
    location / {
        proxy_pass http://ics_backend;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
```

启用并验证：

```bash
sudo ln -s /etc/nginx/sites-available/ics /etc/nginx/sites-enabled/   # Debian 系
sudo nginx -t          # 语法检查，务必先跑
sudo systemctl reload nginx
```

### 保护管理后台

管理后台**自带登录**：访问 `/admin/**` 会跳转到登录页，模型配置、RAG、向量库、
客服配置、知识文档这些接口也都要求带管理员令牌。聊天、会话的**创建**和凭
session_id **读回消息**不需要登录，因为嵌在别人网站上的客服组件没有凭据可给；
但**枚举、查看元数据、关闭**任意会话属于管理接口，同样要求登录。
登录接口自带暴力破解防护：15 分钟内错 5 次锁定 15 分钟起（逐次翻倍，上限 24 小时）。

所以上线时**必须做的两件事**：

```bash
# 1. 换掉签名密钥 —— 保持默认等于任何读过本仓库的人都能伪造管理员令牌
python -c "import secrets; print(secrets.token_urlsafe(48))"
# 把结果写进 .env 的 APP_SECRET_KEY

# 2. 登录后立刻在后台「用户信息」里改掉默认密码 admin / 123456
```

密钥默认值现在**不再只是警告**：`APP_ENV=production` 且 `APP_SECRET_KEY` 还是
默认值时，后端**拒绝启动**（日志会给出生成命令）；开发/测试环境则自动生成一个
随机密钥并持久化到 `backend/data/.secret_key`（0600 权限，重启不会把大家登出）。
默认密码没改时，后台顶部会一直挂着提醒横幅，别忽略。

忘了密码没有邮件找回流程（本项目不配置邮件），在服务器上执行：

```bash
cd backend && python -m scripts.reset_admin_password
```

下面几种做法是**额外的一层**，不是替代品 —— 内置登录挡住的是「谁能改配置」，
这几种挡的是「谁能碰到这个页面」，公网部署建议至少选一种：

**① 只允许特定 IP**（最简单）

```nginx
location /admin/ {
    allow 1.2.3.4;          # 你的办公/家庭固定 IP
    deny all;
    proxy_pass http://ics_backend;
    proxy_set_header Host $host;
}
```

**② HTTP Basic 认证**

```bash
sudo apt install -y apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd admin
```

```nginx
location /admin/ {
    auth_basic "Admin";
    auth_basic_user_file /etc/nginx/.htpasswd;
    proxy_pass http://ics_backend;
    proxy_set_header Host $host;
}
```

同样给写接口加一层（内置鉴权已经挡住了，这里是双保险）：

```nginx
location ~ ^/api/(config|admin|documents|models)(/|$) {
    auth_basic "Admin";
    auth_basic_user_file /etc/nginx/.htpasswd;
    proxy_pass http://ics_backend;
    proxy_set_header Host $host;
}
```

**③ 干脆不对外开放** —— 只在服务器本机或内网访问，通过 SSH 端口转发：

```bash
# 在你自己的电脑上执行
ssh -L 8000:127.0.0.1:8000 user@你的服务器
# 然后本地浏览器打开 http://localhost:8000/admin/
```

这样公网只能访问聊天接口和组件，管理后台完全不暴露。**推荐这个。**

⚠️ 无论选哪种，都不要为了图方便设 `AUTH_ENABLED=false`。那会关掉全部鉴权，
只适合内网里用完就删的临时演示。

---

## HTTPS 证书

Widget 嵌入 HTTPS 站点时，接口也必须是 HTTPS（否则浏览器拦截混合内容），
所以这一步基本是必须的。

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d 你的域名 -d www.你的域名
```

certbot 会自动改好 Nginx 配置并配置续期。验证续期：

```bash
sudo certbot renew --dry-run
```

拿到证书后，把 `.env` 的 `CORS_ORIGINS` 改成 `https://` 开头的域名并重启应用。

---

## 数据库选型：SQLite、MySQL、PostgreSQL 与信创库

| | SQLite（默认） | MySQL / MariaDB | PostgreSQL | 达梦 DM8 |
|---|---|---|---|---|
| 配置 | 零配置 | 需部署 | 需部署 | 需部署（信创栈） |
| 并发写 | 弱（会锁库） | 强 | 强 | 强 |
| 备份 | 拷一个文件 | mysqldump | pg_dump | dexp |
| 适用 | 单机、中小流量 | 多 worker、高并发 | 多 worker、高并发 | 信创合规环境 |
| 驱动 | aiosqlite（已含） | aiomysql（已含） | asyncpg（已含） | 需装信创驱动清单 |

这个库只存**配置、会话记录、文档元数据**（不存向量），数据量很小。
单机部署 SQLite 完全够用。

### MySQL（以及 TiDB / OceanBase MySQL 模式 / GaussDB for MySQL）

```env
DATABASE_URL=mysql+aiomysql://cs_user:密码@localhost:3306/cs_db
```

建库时注意字符集，否则中文会乱码：

```sql
CREATE DATABASE cs_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'cs_user'@'localhost' IDENTIFIED BY '你的密码';
GRANT ALL PRIVILEGES ON cs_db.* TO 'cs_user'@'localhost';
FLUSH PRIVILEGES;
```

TiDB、OceanBase（MySQL 租户）、GaussDB(for MySQL) 走同一协议，把主机端口
换掉即可，驱动不变。

### PostgreSQL 与 PG 系信创库（推荐的信创路线）

人大金仓 KingbaseES、openGauss、瀚高 HighGo、海量 Vastbase G100、华为
GaussDB（PG 兼容版）都兼容 PostgreSQL 协议，统一用 asyncpg：

```env
# PostgreSQL
DATABASE_URL=postgresql+asyncpg://cs_user:密码@localhost:5432/cs_db
# 人大金仓 KingbaseES（建库时选 PG 兼容模式，默认端口 54321）
DATABASE_URL=postgresql+asyncpg://system:密码@localhost:54321/security
```

注意：openGauss 默认的 sha256 认证第三方驱动连不上，需在服务端改成
md5/scram 认证；金仓必须建在 **PG 兼容模式**（原厂 `kingbase8` 方言是同步的，
本项目异步引擎用不了）。

### 达梦 DM8

达梦用原厂异步方言 `dm+dmAsync`（默认端口 5236）。官方只提供 Linux
（x86_64/aarch64，适配麒麟、统信）和 Windows 的编译 wheel：

```bash
pip install -r requirements-xinchuang.txt   # dmPython + dmSQLAlchemy + dmAsync
```

```env
DATABASE_URL=dm+dmAsync://SYSDBA:SYSDBA@localhost:5236/DAMENG
```

> 驱动缺失、填成同步驱动（psycopg2 / pymysql / dmPython）或 scheme 写错时，
> 启动会直接报出该装什么、怎么改连接串。换库前建议先跑
> `python scripts/check_env.py` 预检。

### 换库之后

需要重新建表 —— 打开管理后台点「🚀 一键初始化」，或跑
`python scripts/init_db.py`。**注意配置和文档记录不会自动迁移，需要重新配置和入库。**

---

## 向量库选型与部署

### Chroma（默认，推荐先用这个）

零部署，数据在 `backend/data/chroma_db`。

```env
VECTOR_DB_PROVIDER=chroma
VECTOR_DB_CHROMA_PERSIST_DIR=./data/chroma_db
```

注意：多 worker 写同一目录可能冲突，见上面 [`--workers` 说明](#关于---workers)。

### Qdrant（生产级）

```bash
docker run -d --name qdrant --restart unless-stopped \
  -p 127.0.0.1:6333:6333 \
  -v /opt/qdrant_storage:/qdrant/storage \
  qdrant/qdrant:latest
```

```env
VECTOR_DB_PROVIDER=qdrant
VECTOR_DB_HOST=localhost      # Docker Compose 里写服务名 qdrant
VECTOR_DB_PORT=6333
```

### Milvus

本地小规模可用 **Milvus Lite**（零安装，写 `.db` 文件）：

```env
VECTOR_DB_PROVIDER=milvus
VECTOR_DB_MILVUS_URI=./data/milvus.db
```

生产用 standalone（需 etcd + minio，项目的 `backend/docker-compose.yml` 里有现成配置）：

```bash
cd backend && docker compose up -d milvus
```

```env
VECTOR_DB_PROVIDER=milvus
VECTOR_DB_MILVUS_URI=http://localhost:19530
```

### ⚠️ 换向量库或换 Embedding 模型后

必须做两件事，否则检索结果会错乱或直接报维度不匹配：

1. **重启应用**（向量库客户端在启动时建立连接）
2. **重新入库全部文档** —— 旧向量的维度/存储位置都不同，不会自动迁移

换 Embedding 模型时，先在「模型配置 → 🧬 向量模型」点「🔌 测试并探测维度」，
页面会自动把维度同步到向量库配置。

---

## 首次上线检查清单

按顺序走一遍：

```bash
# 1. 服务活着
curl -fsS https://你的域名/health
# 期望：{"status":"ok","name":"...","env":"production"}

# 2. 首页可访问
curl -sS -o /dev/null -w "%{http_code}\n" https://你的域名/
# 期望：200

# 3. Widget 能加载
curl -sS -o /dev/null -w "%{http_code}\n" https://你的域名/widget/customer-service.js
# 期望：200

# 4. 后台确实要求登录了
curl -sS -o /dev/null -w "%{http_code}\n" https://你的域名/api/config
# 期望：401（不是 200！200 说明鉴权没生效）

# 5. 能登录，并拿到令牌备后面用
TOKEN=$(curl -sS -X POST https://你的域名/api/auth/login \
     -H 'Content-Type: application/json' \
     -d '{"username":"admin","password":"你改过的密码"}' \
     | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
echo "${TOKEN:0:12}…"

# 6. 模型连通（先在管理后台填好 Key）
curl -sS -X POST https://你的域名/api/models/test \
     -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{}'
# 期望：{"ok":true,"message":"测试成功 - 模型回复: OK"}

# 7. Embedding 连通 + 维度确认
curl -sS -X POST https://你的域名/api/models/test-embedding \
     -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{}'
# 期望：{"ok":true,"dim":1024,...}

# 8. SSE 真的是流式（关键）—— 注意这个接口不需要令牌，组件要能匿名调用
curl -N -sS -X POST https://你的域名/api/chat/stream \
     -H 'Content-Type: application/json' -d '{"message":"你好"}'
# 期望：token 事件逐条出现。若等很久才一次性全部吐出 → Nginx 缓冲没关
```

再打开管理后台，逐项确认：

- [ ] 访问 `/admin/` 会跳到登录页，而不是直接进去
- [ ] 用新密码能登录，旧的默认密码已经不好使
- [ ] 顶部没有黄色未初始化横幅，也没有「请尽快修改默认密码」横幅
- [ ] 「模型配置」测试连接通过
- [ ] 「向量模型」测试并探测维度通过，维度与向量库一致
- [ ] 「知识文档」上传一个小文件，状态变成 **就绪**、分片数 > 0
- [ ] 「聊天预览」能问出带链接的答案
- [ ] 问一个知识库外的问题，确认按你设定的[回答策略](../README.md#回答策略严格-rag-还是允许自主回答)响应

---

## 安全加固

上线前逐条过：

- [ ] **`APP_SECRET_KEY` 换成随机值**（`python3 -c "import secrets;print(secrets.token_urlsafe(48))"`）——
      后台登录令牌用它签名，保持默认等于任何人都能伪造管理员身份
- [ ] **改掉后台默认密码 `admin` / `123456`**（登录后在「用户信息」里改）
- [ ] **确认 `AUTH_ENABLED` 没被设成 `false`**
- [ ] **`APP_ENV=production` 且 `APP_DEBUG=false`** —— debug 模式会在报错时泄露栈信息
- [ ] **`CORS_ORIGINS` 改成具体域名**，不要留 `*`
- [ ] **管理后台再加一层访问控制**（内置登录之外，见上面三种方案）
- [ ] **应用只监听 `127.0.0.1`**，公网入口只有 Nginx
- [ ] **数据库/Redis/向量库不映射到公网端口**（用 `127.0.0.1:端口:端口` 形式）
- [ ] **启用 HTTPS**
- [ ] **不用 root 跑应用**
- [ ] **给聊天接口限流**（防刷爆你的 LLM 账单）：

  ```nginx
  # http 块
  limit_req_zone $binary_remote_addr zone=chat:10m rate=20r/m;

  # server 块的 /api/chat/stream 里
  limit_req zone=chat burst=5 nodelay;
  limit_req_status 429;
  ```

- [ ] **确认 `.gitignore` 生效**（`app.db` 存着 API Key，base64 混淆等同明文）：

  ```bash
  git check-ignore -v backend/data/app.db    # 应输出匹配规则
  git ls-files | grep -E "\.env$|app\.db"    # 应无输出
  ```

- [ ] **API Key 改用 KMS / Vault** —— 当前只是 base64 混淆，不是加密
- [ ] **注意 prompt injection** —— 上传的文档内容会进提示词，别放不可信来源的文件
- [ ] **防火墙只开必要端口**：

  ```bash
  sudo ufw allow 22/tcp && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp
  sudo ufw enable
  ```

---

## 备份与恢复

### 要备份什么

| 内容 | 路径 | 重要性 |
|---|---|---|
| 配置 + 会话 + 文档记录 | `backend/data/app.db` | ⭐⭐⭐ 含 API Key |
| 向量数据 | `backend/data/chroma_db`（或 Qdrant/Milvus 卷） | ⭐⭐ 可重新入库重建 |
| 上传的原始文档 | `backend/uploads/` | ⭐⭐ 丢了要重新上传 |
| 环境变量 | `backend/.env` | ⭐⭐⭐ |

### 备份脚本

`/opt/ics/backup.sh`：

```bash
#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/opt/ics/backend
DEST=/opt/backups
KEEP_DAYS=14
STAMP=$(date +%Y%m%d_%H%M%S)

mkdir -p "$DEST"

# SQLite 要用 .backup 而不是 cp —— 直接拷可能拿到写入中的破损文件
if [ -f "$APP_DIR/data/app.db" ]; then
    sqlite3 "$APP_DIR/data/app.db" ".backup '$DEST/app_$STAMP.db'"
fi

tar czf "$DEST/data_$STAMP.tar.gz" \
    -C "$APP_DIR" data uploads .env 2>/dev/null || true

# 清理过期备份
find "$DEST" -name '*.db' -mtime +$KEEP_DAYS -delete
find "$DEST" -name '*.tar.gz' -mtime +$KEEP_DAYS -delete

echo "[$(date '+%F %T')] backup done: $DEST/data_$STAMP.tar.gz"
```

```bash
chmod +x /opt/ics/backup.sh
# 每天 3:17 跑（避开整点，减少和其他任务撞车）
(crontab -l 2>/dev/null; echo "17 3 * * * /opt/ics/backup.sh >> /var/log/ics-backup.log 2>&1") | crontab -
```

MySQL 版本改用：

```bash
docker compose -f docker-compose.prod.yml exec -T mysql \
  mysqldump -ucs_user -p"$MYSQL_PASSWORD" --single-transaction cs_db \
  | gzip > "$DEST/cs_db_$STAMP.sql.gz"
```

### 恢复

```bash
sudo systemctl stop ics          # 或 docker compose stop app
cd /opt/ics/backend
tar xzf /opt/backups/data_20260817_031700.tar.gz -C .
sudo chown -R csapp:csapp data uploads .env
sudo systemctl start ics
curl -fsS http://127.0.0.1:8000/health
```

**验证过备份能恢复，才算有备份。** 建议在测试机上实际走一遍。

---

## 升级与回滚

```bash
cd /opt/ics

# 1. 先备份
./backup.sh

# 2. 记下当前版本，方便回滚
git rev-parse --short HEAD

# 3. 拉新代码
git pull

# 4a. Docker
docker compose -f docker-compose.prod.yml up -d --build

# 4b. systemd
cd backend
sudo -u csapp .venv/bin/pip install -r requirements.txt
sudo systemctl restart ics

# 5. 数据库结构变更会在启动和「一键初始化」时自动补齐缺失列，
#    也可以手动触发：
curl -sS -X POST http://127.0.0.1:8000/api/admin/init

# 6. 验证
curl -fsS http://127.0.0.1:8000/health
```

回滚：

```bash
git checkout <之前的 commit>
docker compose -f docker-compose.prod.yml up -d --build   # 或 systemctl restart ics
```

> 更新了 widget 后，记得把嵌入代码里的 `?v=1` 递增（`?v=2`），
> 否则访客浏览器会继续用缓存的旧版本。

---

## 监控与日志

```bash
# systemd
sudo journalctl -u ics -f
sudo journalctl -u ics --since "1 hour ago" | grep -i error

# Docker
docker compose -f docker-compose.prod.yml logs -f --tail=200 app

# Nginx
sudo tail -f /var/log/nginx/ics.error.log
sudo tail -f /var/log/nginx/ics.access.log
```

### 简易健康检查 + 自动重启

`/opt/ics/healthcheck.sh`：

```bash
#!/usr/bin/env bash
if ! curl -fsS --max-time 10 http://127.0.0.1:8000/health > /dev/null; then
    echo "[$(date '+%F %T')] health check failed, restarting" >&2
    systemctl restart ics
fi
```

```bash
chmod +x /opt/ics/healthcheck.sh
(crontab -l 2>/dev/null; echo "*/5 * * * * /opt/ics/healthcheck.sh >> /var/log/ics-health.log 2>&1") | crontab -
```

systemd 的 `Restart=always` 已能处理进程崩溃；这个脚本兜住"进程活着但不响应"的情况。

### 值得留意的日志

| 日志内容 | 含义 |
|---|---|
| `rate-limited ... 后重试` | 触发厂商限流，正在自动重试（正常） |
| `Embedding stopped at chunk N/M` | 入库因配额中断，可重试续传 |
| `Marking stale processing document as failed` | 有入库任务因重启中断，已标记可重试 |
| `RAG fallback to model knowledge` | 走了自主回答（说明该问题知识库没覆盖） |

---

## 故障排查

<details>
<summary><b>页面打不开 / 502 Bad Gateway</b></summary>

```bash
# 应用是否在跑
sudo systemctl status ics
curl -fsS http://127.0.0.1:8000/health

# 端口是否监听
sudo ss -tlnp | grep 8000

# 看应用日志
sudo journalctl -u ics -n 50
```

502 通常是应用没起来或端口不对，先确认第 2 步能通。
</details>

<details>
<summary><b>回答不是逐字出现，要等很久才一次性显示</b></summary>

Nginx 缓冲没关。确认 `/api/chat/stream` 的 location 里有：

```nginx
proxy_buffering off;
proxy_cache off;
chunked_transfer_encoding off;
```

改完 `sudo nginx -t && sudo systemctl reload nginx`。

用这条命令验证（token 应逐条出现）：

```bash
curl -N -sS -X POST https://你的域名/api/chat/stream \
     -H 'Content-Type: application/json' -d '{"message":"你好"}'
```
</details>

<details>
<summary><b>上传文档报 413 Request Entity Too Large</b></summary>

Nginx 的 `client_max_body_size` 小于文件大小。改成比 `APP_MAX_UPLOAD_MB` 稍大：

```nginx
client_max_body_size 25M;
```
</details>

<details>
<summary><b>入库很慢或反复失败</b></summary>

多半是 embedding 厂商配额限制（实测火山方舟的配额是长周期累计的）。

本项目已做断点续传，进度不会丢。大知识库用脚本挂后台：

```bash
cd /opt/ics/backend
sudo -u csapp nohup .venv/bin/python scripts/ingest_retry.py \
     --cooldown 60 > /tmp/ingest.log 2>&1 &
tail -f /tmp/ingest.log
```

想快很多就换配额宽松的 embedding 服务。注意换模型后维度会变，
需同步改向量库维度并重新入库全部文档。
</details>

<details>
<summary><b>Widget 嵌到网站后不显示 / 控制台报 CORS 错误</b></summary>

1. `.env` 的 `CORS_ORIGINS` 是否包含嵌入方的域名（含协议和端口）
2. 嵌入方是 HTTPS 站点时，`apiUrl` 也必须是 HTTPS
3. 改完 `CORS_ORIGINS` 要重启应用

```bash
curl -sSI -X OPTIONS https://你的域名/api/chat/stream \
     -H "Origin: https://嵌入方域名" | grep -i access-control
```
</details>

<details>
<summary><b>改了前端文件但浏览器还是旧的</b></summary>

后端对 `/admin`、`/widget`、`/embed` 已设 `Cache-Control: no-store`，正常刷新即可。

如果中间还有 CDN，需要在 CDN 侧刷新缓存，或给嵌入 URL 递增 `?v=`。
</details>

<details>
<summary><b>换了向量库后检索结果不对</b></summary>

换库/换 embedding 模型后必须**重启应用 + 重新入库全部文档**。
旧向量的维度和存储位置都不同，不会自动迁移。

先在「模型配置 → 🧬 向量模型」点「测试并探测维度」，确认维度与向量库一致。
</details>

<details>
<summary><b>磁盘满了</b></summary>

```bash
du -sh /opt/ics/backend/data/* /opt/ics/backend/uploads /opt/backups
```

常见占用大户：向量数据（12,596 片段 ≈ 163 MB）、上传的原始文档、过期备份。

- 删掉不再需要的文档（管理后台「知识文档」里删，会同时清理向量）
- 调小 `backup.sh` 里的 `KEEP_DAYS`
- Docker 用户跑 `docker system prune -a` 清理旧镜像
</details>

---

## 相关文档

- [README.md](../README.md) — 项目总览、页面导览、配置项参考
- [API.md](API.md) — 接口详细文档
- [EMBED_GUIDE.md](EMBED_GUIDE.md) — 嵌入到网站/小程序/桌面端
