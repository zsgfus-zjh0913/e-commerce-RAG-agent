# 生产部署指南

## 一、本地开发模式

```bash
pip install -r requirements.txt
python app.py
# 访问 http://127.0.0.1:5000
```

## 二、生产部署（Docker + Nginx）

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
# 编辑 .env，设置：
# DEEPSEEK_API_KEY=your_api_key
# SECRET_KEY=your_random_secret
# JWT_SECRET=your_jwt_secret
```

### 3. 迁移数据到数据库（首次）

```bash
python migrate.py
```

### 4. Docker 部署

```bash
docker-compose up -d
# 服务启动：
# - app: gunicorn (端口 5000)
# - nginx: 反向代理 + SSL (端口 80/443)
```

## 三、生产部署（裸机 + Nginx）

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 迁移数据

```bash
python migrate.py
```

### 3. 启动 Gunicorn

```bash
gunicorn -c gunicorn_config.py app:app
```

### 4. 配置 Nginx

```bash
sudo cp nginx.conf /etc/nginx/sites-available/ecommerce-cs
sudo ln -s /etc/nginx/sites-available/ecommerce-cs /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

### 5. HTTPS 证书

```bash
sudo certbot --nginx -d your-domain.com
```

## 四、安全配置说明

| 安全措施 | 说明 |
|---------|------|
| bcrypt 密码哈希 | cost=12，不可逆 |
| JWT 鉴权 | 24h 过期，支持黑名单登出 |
| 安全响应头 | HSTS/CSP/X-Frame-Options/X-Content-Type-Options |
| 接口限流 | 登录 5次/分，聊天 30次/分，上传 10次/分 |
| 输入消毒 | HTML 转义，长度限制，XSS/SQL 注入检测 |
| 文件验证 | 类型白名单，大小限制 50MB，路径穿越防护 |
| Cookie 安全 | httponly + samesite=Strict |
| HTTPS | Nginx SSL 终端，强制 HTTP→HTTPS 跳转 |

## 五、数据库管理

### 查看数据

```bash
sqlite3 ecommerce.db
.tables
SELECT * FROM products;
SELECT * FROM faq;
SELECT * FROM users;
```

### 添加商品

```sql
INSERT INTO products (product_id, name, brand, category, price, description, stock, attributes, created_at)
VALUES ('P009', '新产品', '品牌', '分类', '99.00', '描述', '100件', '{}', strftime('%s','now'));
```

### 删除索引重建

```bash
rm -rf index/
# 重启服务，自动从数据库重建
```

## 六、水平扩展

```yaml
# docker-compose.yml 增加 app 实例
services:
  app:
    deploy:
      replicas: 3
```

```nginx
# nginx.conf 增加上游
upstream ecommerce_backend {
    server app:5000;
    least_conn;  # 最少连接负载均衡
}
```

## 七、备份

```bash
# 数据库备份
sqlite3 ecommerce.db ".backup /backup/ecommerce_$(date +%Y%m%d).db"

# 定时备份 (crontab)
0 2 * * * sqlite3 /app/ecommerce.db ".backup /backup/ecommerce_$(date +\%Y\%m\%d).db"
```
