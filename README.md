# 电商智能客服系统

基于 RAG 检索增强生成 + LLM 的电商智能客服系统，结合了 ecommerce-agent 和 RAG 项目的功能。

## 核心功能

- **用户登录/注册** — 账号密码登录，支持性别选择，可重复登录
- **Query 改写** — 用户输入后，大模型先改写优化用户问题，再进行检索和回答
- **离线 RAG 检索** — 未配置 API Key 时自动切换为离线 RAG 检索模式
- **多格式文档上传** — 支持 .txt .md .csv .json .docx .pdf .xlsx .pptx .html
- **商品图片识别** — 上传实拍图后通过 PaddleOCR + 中文 CLIP 检索相似商品，并结合商品文本 RAG 回答材质、尺码、型号和价格
- **历史记录** — 侧边栏展示对话历史，可点击恢复、删除
- **可收起侧边栏** — 侧边栏可收起/展开，适配桌面和移动端
- **流式回答** — SSE 流式输出，实时显示改写查询和回答内容
- **商品客服** — 商品信息查询、更换商品、退换货处理、物流咨询
- **实时业务查询（Function Calling）** — 订单/物流/售后工单进度/实时库存走 LLM 工具调用直查业务库，不再依赖知识库陈旧数据

## 实时业务查询（Function Calling）流程

用户问「订单/快递/售后进度/库存」类问题时，链路如下：

1. 关键词门控判定疑似业务数据问题（避免普通问答产生额外 LLM 调用）
2. LLM 携带工具定义决策：是否需要调用工具、抽取参数
3. 执行工具查询业务库（账号由服务端绑定，用户无法越权查他人数据）
4. LLM 基于工具返回的实时数据生成回答并流式返回

- 无 API Key 或 LLM 不可用时自动降级为确定性查询（直接查库拼文本）。
- 知识型问题（政策/尺码/介绍）不会触发业务工具，仍走 RAG 链路。
- **接入真实业务系统**：只需在 `business_tools.py` 中改写 4 个执行器
  （`_exec_orders / _exec_logistics / _exec_after_sales / _exec_product`）的查询来源，
  将其指向真实业务库的表或内部 API 即可，工具 schema 与上层链路无需改动。
- Function Calling 需要模型支持 `tools` 参数（DeepSeek `deepseek-chat` 支持；
  若走其他 OpenAI 兼容网关，请确认其支持函数调用）。

## 技术架构

| 模块 | 技术 |
|------|------|
| 后端框架 | Flask |
| RAG 引擎 | sentence-transformers (BAAI/bge-small-zh) + TF-IDF 混合检索 |
| 重排序 | BAAI/bge-reranker-base Cross-Encoder |
| LLM | DeepSeek API (deepseek-chat) |
| 前端 | HTML + CSS + JavaScript (SSE 流式) |
| 文档解析 | python-docx / pdfplumber / openpyxl / python-pptx |
| 用户认证 | 自定义 Token 认证 + Cookie |

## Query 改写流程

1. 用户输入问题
2. LLM 改写优化用户问题（补充上下文、规范表达）
3. 使用改写后的查询进行 RAG 检索
4. LLM 基于改写查询 + RAG 上下文生成回答

未配置 API Key 时，跳过改写和 LLM 回答，直接返回 RAG 检索结果。

## 头像设置

- 客服头像：🐂（牛马）
- 用户头像：👦（男生） / 👧（女生），根据注册时选择的性别显示

## 快速开始

1. 安装依赖：
   ```bash
   pip install -r requirements.txt
   ```

2. 配置 API Key（可选，不配置则使用离线 RAG）：
   ```bash
   # 方式1：环境变量
   export DEEPSEEK_API_KEY="your-api-key"

   # 方式2：启动后在侧边栏输入
   ```

3. 启动应用：
   ```bash
   python app.py
   ```

4. 访问 http://127.0.0.1:5000

## 项目结构

```
电商智能客服/
├── app.py              # Flask 主程序
├── rag_engine.py       # RAG 混合检索引擎
├── llm_client.py       # LLM 客户端（Query 改写 + Function Calling）
├── business_tools.py   # 业务数据工具层（订单/物流/售后/库存，接入真实业务库的适配点）
├── auth_manager.py     # 用户认证管理
├── requirements.txt    # 依赖
├── config.json         # API Key 配置
├── .env               # 环境变量
├── data/              # 知识库文档
│   ├── faq.txt
│   ├── 商品信息.md
│   ├── shipping_policy.md
│   └── size_chart.csv
├── index/             # RAG 索引（自动生成）
├── histories/         # 用户对话历史（自动生成）
├── templates/
│   ├── login.html     # 登录/注册页面
│   └── index.html     # 聊天主界面
├── static/
│   ├── css/style.css  # 样式
│   └── js/main.js     # 交互逻辑
└── users.json         # 用户数据（自动生成）
```

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | / | 聊天主界面（需登录） |
| GET | /login | 登录页面 |
| POST | /api/register | 注册 |
| POST | /api/login | 登录 |
| POST | /api/logout | 退出登录 |
| GET | /api/user | 获取当前用户信息 |
| GET | /api/session | 创建会话 |
| GET/POST | /api/settings | 获取/设置 API Key |
| POST | /api/query | 聊天查询（SSE 流式） |
| POST | /api/upload | 上传文档 |
| GET | /api/documents | 文档列表 |
| POST | /api/delete | 删除文档 |
| GET | /api/stats | 引擎统计 |
| GET | /api/history | 历史记录列表 |
| GET | /api/history/:id | 获取历史对话 |
| DELETE | /api/history/:id | 删除历史对话 |
