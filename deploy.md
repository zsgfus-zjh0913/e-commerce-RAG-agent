# 生产部署指南

## 一、本地开发模式（SQLite + 内存，零配置）

```bash
pip install -r requirements.txt
python app.py
# 访问 http://127.0.0.1:5000
# 健康检查 http://127.0.0.1:5000/health
# 坐席工作台 http://127.0.0.1:5000/agent/login
```

## 二、生产部署（Docker + MySQL + Redis + Nginx）

### 1. 准备 SSL 证书

```bash
mkdir ssl
# 使用 Let's Encrypt 免费证书
certbot certonly --standalone -d your-domain.com
cp /etc/letsencrypt/live/your-domain.com/fullchain.pem ssl/cert.pem
cp /etc/letsencrypt/live/your-domain.com/privkey.pem ssl/key.pem
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env：
# DEEPSEEK_API_KEY=your_api_key
# DATABASE_URL=mysql+pymysql://ecommerce:password@db:3306/ecommerce?charset=utf8mb4
# REDIS_URL=redis://redis:6379/0
# APP_ENV=production
# FLASK_DEBUG=false
# SECRET_KEY=<32位以上随机字符串>
# JWT_SECRET=<另一条32位以上随机字符串>
# AGENT_JWT_SECRET=<第三条32位以上随机字符串>
```

### 3. 创建数据库（MySQL）

```sql
CREATE DATABASE ecommerce CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'ecommerce'@'%' IDENTIFIED BY 'password';
GRANT ALL PRIVILEGES ON ecommerce.* TO 'ecommerce'@'%';
FLUSH PRIVILEGES;
```

### 4. 迁移数据（SQLite → MySQL）

```bash
# 先运行本地迁移创建初始数据
python migrate.py
python migrate_enterprise.py

# 再迁移到 MySQL
set DATABASE_URL=mysql+pymysql://root:password@localhost:3306/ecommerce?charset=utf8mb4
python migrate_to_mysql.py
```

### 5. Docker 一键部署

```bash
docker-compose up -d --build
# 服务启动：
# - app:    gunicorn (端口 5000) —— 聊天/坐席主应用，gthread 8 线程抗长连接
# - rag:    独立检索服务 (端口 5001) —— embedding/reranker 模型仅此进程加载，
#            Qdrant 本地嵌入模式存储向量
# - db:     MySQL 8.0 (端口 3306)
# - redis:  Redis 7 (端口 6379) —— 任务事件缓冲/缓存/限流
# - nginx:  反向代理 + SSL (端口 80/443)
```

### 6. 验证

```bash
# 健康检查
curl http://localhost/health

# 预期输出：
# {"status":"ok","database":{"type":"external","url":"localhost:3306/ecommerce"},
#  "storage":{"redis_enabled":true,"storage_mode":"redis"},...}

# 检索服务健康检查
curl http://localhost:5001/health
# {"status":"ok","chunks":196,"mode":"vector","store_type":"QdrantStore",...}
```

## 三、生产部署（裸机 + MySQL + Redis + Nginx）

### 1. 安装 MySQL + Redis

```bash
# MySQL
sudo apt install mysql-server
sudo mysql_secure_installation

# Redis
sudo apt install redis-server
sudo systemctl enable redis-server
```

### 2. 安装 Python 依赖

```bash
pip install -r requirements.txt
```

### 3. 配置环境变量

```bash
export DATABASE_URL="mysql+pymysql://ecommerce:password@localhost:3306/ecommerce?charset=utf8mb4"
export REDIS_URL="redis://localhost:6379/0"
export APP_ENV="production"
export FLASK_DEBUG="false"
export SECRET_KEY="$(openssl rand -hex 32)"
export JWT_SECRET="$(openssl rand -hex 32)"
export AGENT_JWT_SECRET="$(openssl rand -hex 32)"
export DEEPSEEK_API_KEY="your-api-key"
```

### 4. 迁移数据

```bash
python migrate.py
python migrate_enterprise.py
python migrate_to_mysql.py
```

### 5. 启动 Gunicorn

```bash
# 主应用：gthread 抗长连接（任务模式已解耦 LLM 生成）
export GUNICORN_WORKER_CLASS=gthread GUNICORN_THREADS=8 GUNICORN_WORKERS=4
gunicorn -c gunicorn_config.py app:app

# 独立检索服务（必须先于主应用就绪或与其并行启动，首次会同步数据库并构建向量索引）
# 本地 Qdrant 单进程锁：必须单 worker
gunicorn --bind 0.0.0.0:5001 --workers 1 --timeout 120 rag_service:app
```

> 若未配置 `RAG_SERVICE_URL`，主应用自动回退为进程内 RAG 引擎（功能可用，
> 但 embedding 模型会在每个 worker 加载一次，高并发能力下降——生产不建议）。

### 6. 配置 Nginx + HTTPS

```bash
sudo cp nginx.conf /etc/nginx/sites-available/ecommerce-cs
sudo ln -s /etc/nginx/sites-available/ecommerce-cs /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d your-domain.com
```

## 四、环境变量说明

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `DATABASE_URL` | 数据库连接（留空=SQLite） | 空 |
| `REDIS_URL` | Redis 连接；生产环境必须配置，用于限流、坐席黑名单、任务事件缓冲和会话共享 | 空（仅开发内存降级） |
| `DEEPSEEK_API_KEY` | DeepSeek API Key | 空（离线模式） |
| `RAG_SERVICE_URL` | 检索服务地址（`http://rag:5001`）；配置后主应用走 HTTP 检索，模型不在主进程加载 | 空（进程内 RAG，降级） |
| `CHAT_TASK_MODE` | `1`=任务模式：提交秒回 task_id，LLM 生成后台执行，SSE 事件增量拉取 | 空（直连 SSE） |
| `QDRANT_LOCAL_PATH` | Qdrant 本地嵌入模式路径（单进程锁，rag 服务必须单 worker） | 空（NumpyTfidfStore） |
| `QDRANT_URL` | 远程 Qdrant 服务地址；与 LOCAL 二选一，支持更大并发 | 空 |
| `POOL_SIZE` / `POOL_MAX_OVERFLOW` | MySQL 连接池大小/溢出上限（多 worker 时按预算） | 10 / 20 |
| `GUNICORN_WORKERS` / `GUNICORN_THREADS` | Gunicorn worker 数 / 每 worker 线程数（gthread 模式抗长连接） | cpu×2+1 / 1 |
| `APP_ENV` | 运行环境，生产必须为 `production` | `development` |
| `SECRET_KEY` | Flask Session 签名密钥 | 仅开发默认 |
| `JWT_SECRET` | 用户 JWT 签名密钥 | 仅开发默认 |
| `AGENT_JWT_SECRET` | 坐席 JWT 签名密钥 | 仅开发默认 |
| `FLASK_DEBUG` | 调试模式 | false |
| `PORT` | 监听端口 | 5000 |

## 五、安全配置

| 安全措施 | 说明 |
|---------|------|
| bcrypt 密码哈希 | cost=12，不可逆 |
| JWT 鉴权 | 用户 24h + 坐席 12h，黑名单登出 |
| 安全响应头 | HSTS/CSP/X-Frame-Options/X-Content-Type-Options |
| 接口限流 | 登录 5次/分，聊天 30次/分，上传 10次/分 |
| Nginx 限流 | 4 级限流区域（api/auth/chat/upload） |
| 输入消毒 | HTML 转义，长度限制，XSS/SQL 注入检测 |
| 文件验证 | 类型白名单，50MB 限制，路径穿越防护 |
| Cookie 安全 | httponly + samesite=Strict |
| HTTPS | TLS 1.2/1.3，HTTP 强制跳转 HTTPS |
| 健康检查 | /health 端点，Docker HEALTHCHECK |

## 六、数据库管理

### MySQL 连接

```bash
mysql -u ecommerce -p ecommerce
SHOW TABLES;
SELECT * FROM products;
SELECT * FROM orders;
SELECT * FROM tickets;
SELECT * FROM audit_logs;
```

### 添加商品

```sql
INSERT INTO products (product_id, name, brand, category, price, description, stock, attributes, created_at)
VALUES ('P009', '新产品', '品牌', '分类', '99.00', '描述', '100件', '{}', UNIX_TIMESTAMP());
```

### 重建 RAG 索引

```bash
# NumpyTfidfStore 模式
rm -rf index/
# Qdrant 本地模式：删除向量库目录（qdrant_data 在 docker-compose 中为独立卷）
rm -rf qdrant_data/
# 重启 rag 服务，自动从数据库全量重建
```

## 七、水平扩展

```yaml
# docker-compose.yml
services:
  app:
    deploy:
      replicas: 3
```

```nginx
# nginx.conf
upstream ecommerce_backend {
    server app1:5000;
    server app2:5000;
    server app3:5000;
    least_conn;
    keepalive 32;
}
```

## 八、备份

### MySQL 备份

```bash
# 手动备份
mysqldump -u ecommerce -p ecommerce > backup_$(date +%Y%m%d).sql

# 定时备份 (crontab)
0 2 * * * mysqldump -u ecommerce -pPASSWORD ecommerce > /backup/ecommerce_$(date +\%Y\%m\%d).sql

# 恢复
mysql -u ecommerce -p ecommerce < backup_20260829.sql
```

### Redis 持久化

```bash
# Redis 已配置 appendonly 持久化
# 备份 RDB 文件
cp /var/lib/redis/dump.rdb /backup/redis_$(date +%Y%m%d).rdb
```

## 九、监控

### 健康检查

```bash
curl http://localhost/health | python -m json.tool
```

### 日志查看

```bash
# 应用日志
tail -f logs/app_$(date +%Y%m%d).log

# Nginx 日志
tail -f /var/log/nginx/access.log
tail -f /var/log/nginx/error.log

# 坐席工作台 → 系统日志 Tab
# 访问 http://your-domain.com/agent/dashboard
```

### 审计日志

```sql
SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT 50;
```
