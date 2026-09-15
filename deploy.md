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
# SECRET_KEY=your_random_secret
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
docker-compose up -d
# 服务启动：
# - app:    gunicorn (端口 5000)
# - db:     MySQL 8.0 (端口 3306)
# - redis:  Redis 7 (端口 6379)
# - nginx:  反向代理 + SSL (端口 80/443)
```

### 6. 验证

```bash
# 健康检查
curl http://localhost/health

# 预期输出：
# {"status":"ok","database":{"type":"external","url":"localhost:3306/ecommerce"},
#  "storage":{"redis_enabled":true,"storage_mode":"redis"},...}
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
export SECRET_KEY="your-random-secret-key"
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
gunicorn -c gunicorn_config.py app:app
```

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
| `REDIS_URL` | Redis 连接（留空=内存） | 空 |
| `DEEPSEEK_API_KEY` | DeepSeek API Key | 空（离线模式） |
| `SECRET_KEY` | JWT 签名密钥 | 内置默认 |
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
rm -rf index/
# 重启服务，自动从数据库重建
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
