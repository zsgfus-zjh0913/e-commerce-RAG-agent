import json
import os
import requests


class LLMClient:
    """LLM 客户端：支持 Query 改写 + 流式对话回答。"""

    BASE_URL = os.environ.get(
        "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
    )
    MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

    REWRITE_SYSTEM_PROMPT = (
        "你是一个电商客服系统的查询改写助手。请根据用户的原始问题和对话历史，"
        "将用户的问题改写为更加清晰、具体、便于知识库检索的查询。\n\n"
        "规则：\n"
        "1. 理解用户的口语化表达，转化为规范的查询\n"
        "   （例如：「这个多少钱」→「商品P001纯棉圆领短袖T恤的价格是多少」）\n"
        "2. 补充隐含的上下文信息（如上文提到的商品）\n"
        "3. 如果原始问题已经足够清晰，可以保持不变\n"
        "4. 如果用户的问题是对话性问题（如「你好」「你是谁」「你能做什么」「谢谢」等），"
        "请直接输出原问题，不做改写\n"
        "5. 只输出改写后的查询，不要添加任何解释\n"
        "6. 使用中文\n"
    )

    ANSWER_SYSTEM_PROMPT = (
        "你是「暖小助」，一个专业的电商智能客服助手。\n\n"
        "【你的身份】\n"
        "你是电商平台的客服助手，名叫「暖小助」，头像是🌸。\n"
        "你的主要职责是为客户提供商品咨询和售后服务。\n\n"
        "【你的能力】\n"
        "1. 商品信息查询 — 查询商品详情、价格、尺码、库存、参数等\n"
        "2. 商品更换建议 — 换货政策、换码流程咨询\n"
        "3. 退换货处理 — 退货政策、退款进度查询\n"
        "4. 物流咨询 — 发货时间、运费、配送时效\n"
        "5. 尺码推荐 — 根据客户身材推荐合适尺码\n\n"
        "【回答规则】\n"
        "1. 当用户询问「你是谁」「你叫什么」等身份问题时，介绍你是客服助手「暖小助」及你的职责\n"
        "2. 当用户询问「你能做什么」「你能为我做什么」等能力问题时，列出你的能力清单\n"
        "3. 当用户只是打招呼（你好、嗨等），简短问候并询问需要什么帮助\n"
        "4. 对于商品相关问题，优先依据下方知识库内容回答，不可编造不存在的商品信息\n"
        "5. 理解口语化表达并映射到知识库（男士=男装、一米八=180cm、多大码=适合什么尺码等）\n"
        "6. 若知识库中确实没有相关信息，请直接说明，不要编造答案\n"
        "7. 回答要简洁、准确、条理清晰\n"
        "8. 涉及尺码、价格等具体数据时，请清晰列出\n"
        "9. 禁止重复用户的问题或复述用户的问题，直接给出回答内容\n"
        "10. 只回答用户问到的商品信息，不要把不相关的商品信息也列出来\n"
        "11. 这是多轮对话，用户的问题可能引用上文，请结合对话历史理解用户意图\n"
        "12. 使用中文回答\n\n"
        "13. 用户询问有哪些商品时，使用“目前在售卖的产品有……”这种自然表达，"
        "不要回复“当前知识库里关于商品的信息就这些”。\n\n"
        "知识库检索结果：\n{context}"
    )

    TOOLS_SYSTEM_PROMPT = (
        "你是电商智能客服「暖小助」的业务调度助手，负责判断用户问题是否需要查询实时业务数据，"
        "并选择正确的工具调用。\n\n"
        "可调用工具：\n"
        "- query_orders：查订单（列表/状态/金额）\n"
        "- query_logistics：查物流发货信息（快递单号）\n"
        "- query_after_sales：查售后/退款工单处理进度\n"
        "- query_product：实时查商品库存/价格\n\n"
        "规则：\n"
        "1. 只有明显涉及「我的订单 / 快递物流 / 发货收货 / 退款售后进度 / 实时库存」"
        "这类需要查业务系统的问题才调用工具\n"
        "2. 商品介绍、购物政策、尺码等知识型问题**不要**调用任何工具\n"
        "3. 用户没有给具体订单号时，可以省略 order_id 参数查询其最近订单\n"
        "4. 绝对不要输出用户名相关参数，系统已绑定当前登录用户\n"
        "5. 如果缺少关键信息（例如不知道要查哪个商品），直接输出一句需要向用户澄清的问题，"
        "不要调用工具\n"
        "6. 只输出必要的工具调用或澄清问题，不要多余解释\n"
    )

    TOOLS_ANSWER_SYSTEM_PROMPT = (
        "你是电商智能客服「暖小助」🌸。你刚刚通过业务系统工具查询到了用户订单/物流/"
        "售后/库存的**实时数据**，请据此回答用户的问题。\n\n"
        "规则：\n"
        "1. 只依据工具返回的数据作答，不得编造数据中不存在的信息（如没有快递单号就别说有）\n"
        "2. 数据为空或未找到时如实说明，并给出下一步建议（提供订单号 / 联系人工客服等）\n"
        "3. 若有多个订单/工单，分条列出，保持简洁清晰\n"
        "4. 使用中文回答\n"
    )

    ERROR_TRANSLATIONS = {
        "insufficient balance": "API 账户余额不足，请前往平台充值",
        "invalid api key": "API 密钥无效，请检查密钥是否正确",
        "rate limit": "API 调用频率超限，请稍后重试",
        "model not found": "模型名称错误，请检查模型配置",
    }

    def __init__(self, api_key):
        self.api_key = api_key

    def _translate_error(self, error_msg):
        lower = error_msg.lower()
        for en, zh in self.ERROR_TRANSLATIONS.items():
            if en in lower:
                return zh
        return error_msg

    def _call_api(self, messages, stream=False, max_tokens=1024, tools=None):
        """调用 LLM API 的公共方法（tools 非空时携带函数调用定义）。"""
        payload = {
            "model": self.MODEL,
            "messages": messages,
            "stream": stream,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload.setdefault("tool_choice", "auto")
        try:
            response = requests.post(
                f"{self.BASE_URL}/chat/completions",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                json=payload,
                stream=stream,
                timeout=60,
            )
        except requests.exceptions.ConnectionError:
            return {"error": "无法连接到 API 服务，请检查网络"}
        except requests.exceptions.Timeout:
            return {"error": "API 请求超时，请稍后重试"}

        if response.status_code == 401:
            return {"error": "API 密钥无效，请检查密钥是否正确"}
        if response.status_code == 429:
            return {"error": "API 调用频率超限，请稍后重试"}
        if response.status_code != 200:
            try:
                err = response.json().get("error", {}).get("message", "")
            except Exception:
                err = f"HTTP {response.status_code}"
            return {"error": self._translate_error(err)}

        if stream:
            return {"stream": response}
        try:
            return response.json()
        except Exception:
            return {"error": "API 响应解析失败"}

    def rewrite_query(self, question, history=None):
        """调用 LLM 改写用户查询。返回改写后的查询文本。"""
        messages = [
            {"role": "system", "content": self.REWRITE_SYSTEM_PROMPT},
        ]
        if history:
            for msg in history[-4:]:
                content = msg.get("content", "")
                if len(content) > 300:
                    content = content[:300] + "..."
                messages.append({
                    "role": msg["role"],
                    "content": content,
                })
        messages.append({"role": "user", "content": question})

        result = self._call_api(messages, stream=False, max_tokens=256)
        if "error" in result:
            return None, result["error"]
        try:
            rewritten = result["choices"][0]["message"]["content"].strip()
            if not rewritten:
                return question, None
            return rewritten, None
        except (KeyError, IndexError):
            return question, "改写响应解析失败"

    def _build_answer_messages(self, question, context_chunks, history=None):
        """构建回答阶段的对话消息列表。"""
        if context_chunks:
            context = "\n\n---\n\n".join(
                f"[来源: {c['source']}] {c['text']}" for c in context_chunks
            )
        else:
            context = "（本次检索未找到与用户问题相关的知识库内容）"
        system_content = self.ANSWER_SYSTEM_PROMPT.format(context=context)
        messages = [{"role": "system", "content": system_content}]
        if history:
            for msg in history[-6:]:
                content = msg.get("content", "")
                if len(content) > 500:
                    content = content[:500] + "..."
                messages.append({
                    "role": msg["role"],
                    "content": content,
                })
        messages.append({"role": "user", "content": question})
        return messages

    def chat_stream(self, question, context_chunks, history=None):
        """流式调用，逐块 yield 文本。"""
        messages = self._build_answer_messages(question, context_chunks, history)
        result = self._call_api(messages, stream=True, max_tokens=1024)
        if "error" in result:
            yield f"__ERROR__: {result['error']}"
            return
        if "stream" not in result:
            yield "__ERROR__: 未知错误"
            return

        response = result["stream"]
        for line in response.iter_lines():
            if not line:
                continue
            line = line.decode("utf-8")
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data.strip() == "[DONE]":
                break
            try:
                chunk = json.loads(data)
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content", "")
                if content:
                    yield content
            except (json.JSONDecodeError, IndexError, KeyError):
                continue

    # ── Function Calling（业务工具调用）──────────────────────

    @staticmethod
    def _parse_content(result):
        """从非流式响应中提取首个 message。返回 (message, error)。"""
        if "error" in result:
            return None, result["error"]
        try:
            msg = result["choices"][0]["message"]
        except (KeyError, IndexError):
            return None, "模型响应解析失败"
        return msg, None

    def decide_tools(self, question, history=None, tool_schemas=None):
        """第一轮决策：携带工具定义询问模型是否需要调用业务工具。

        返回 (decision, error)：
          decision = {
            "content": 模型直接输出的文本（可能为空），
            "raw_message": 模型原始 message（供续聊时透传 tool_calls），
            "tool_calls": [{"id", "name", "arguments"(dict), "raw"}...],
          }
        若 error 非空则 decision 为 None。
        """
        messages = [
            {"role": "system", "content": self.TOOLS_SYSTEM_PROMPT},
        ]
        if history:
            for msg in history[-4:]:
                content = msg.get("content", "")
                if len(content) > 400:
                    content = content[:400] + "..."
                messages.append({"role": msg["role"], "content": content})
        messages.append({"role": "user", "content": question})

        result = self._call_api(
            messages, stream=False, max_tokens=512, tools=tool_schemas
        )
        msg, error = self._parse_content(result)
        if error or msg is None:
            return None, error

        content = msg.get("content") or ""
        calls = []
        for raw in (msg.get("tool_calls") or []):
            fn = raw.get("function") or {}
            name = fn.get("name", "")
            raw_args = fn.get("arguments") or ""
            try:
                arguments = json.loads(raw_args) if raw_args.strip() else {}
            except Exception:
                arguments = {"_parse_error": raw_args[:500]} if raw_args.strip() else {}
            calls.append({
                "id": raw.get("id", ""),
                "name": name,
                "arguments": arguments,
                "raw": raw,
            })
        return {"content": content, "raw_message": msg, "tool_calls": calls}, None

    def answer_with_tools(
        self, question, history=None,
        assistant_tool_message=None, tool_messages=None, max_tokens=1024,
    ):
        """工具执行完成后，生成最终回答（非流式，返回完整文本）。

        参数：
          assistant_tool_message: 决策轮返回的原始 assistant message（含 tool_calls），
            按 OpenAI/DeepSeek 协议必须透传；
          tool_messages: 每个工具的结果，{"role": "tool", "tool_call_id": id,
            "content": json 字符串}。
        返回 (text, error)。
        """
        messages = [
            {"role": "system", "content": self.TOOLS_ANSWER_SYSTEM_PROMPT},
        ]
        if history:
            for msg in history[-6:]:
                content = msg.get("content", "")
                if len(content) > 500:
                    content = content[:500] + "..."
                messages.append({"role": msg["role"], "content": content})
        messages.append({"role": "user", "content": question})

        if assistant_tool_message:
            # 透传含 tool_calls 的 assistant 消息（content 置空字符串更稳妥）
            passed = dict(assistant_tool_message)
            passed["content"] = passed.get("content") or ""
            messages.append(passed)
        if tool_messages:
            messages.extend(tool_messages)

        result = self._call_api(messages, stream=False, max_tokens=max_tokens)
        msg, error = self._parse_content(result)
        if error or msg is None:
            return None, error
        text = (msg.get("content") or "").strip()
        return text, None
