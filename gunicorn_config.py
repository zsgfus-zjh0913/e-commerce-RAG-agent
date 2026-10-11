import os
import multiprocessing

# ── 电商智能客服 Gunicorn 生产配置 ──
# 任务模式（CHAT_TASK_MODE=1）下：
#   - POST /api/query 秒回 task_id，LLM 生成在后台线程池执行
#   - 长连接仅做事件缓冲转发，worker 不再被生成/检索长时间占死
# 因此 timeout 可收紧，快速失败利于负载均衡摘除与健康检查。
#
# 若单机 QPS 继续上探（如 >2000 并发连接），可升级为线程 worker：
#   worker_class = "gthread"
#   threads = 8          # 每 worker 8 线程，长连接转发更省进程
# 或 gevent（需 monkey patch，注意 pymysql/SQLAlchemy 兼容）：
#   worker_class = "gevent"
#   worker_connections = 2000

bind = "0.0.0.0:5000"
workers = int(os.environ.get("GUNICORN_WORKERS", str(multiprocessing.cpu_count() * 2 + 1)))
# 生产抗长连接：通过环境变量启用 gthread（如 GUNICORN_WORKER_CLASS=gthread GUNICORN_THREADS=8）
worker_class = os.environ.get("GUNICORN_WORKER_CLASS", "sync")
threads = int(os.environ.get("GUNICORN_THREADS", "1"))
worker_connections = 1000
# 任务解耦后无长计算：60s 未活动即回收，防止僵死连接堆积
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "60"))
keepalive = 5
max_requests = 1000
max_requests_jitter = 50
preload_app = True
accesslog = "-"
errorlog = "-"
loglevel = "info"
proc_name = "ecommerce-cs"

# 优雅重启
graceful_timeout = 30

# 日志格式
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(L)s'
