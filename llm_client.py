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
        "你是「牛马」，一个专业的电商智能客服助手。\n\n"
        "【你的身份】\n"
        "你是电商平台的客服助手，名叫「牛马」，头像是一头牛🐂。\n"
        "你的主要职责是为客户提供商品咨询和售后服务。\n\n"
        "【你的能力】\n"
        "1. 商品信息查询 — 查询商品详情、价格、尺码、库存、参数等\n"
        "2. 商品更换建议 — 换货政策、换码流程咨询\n"
        "3. 退换货处理 — 退货政策、退款进度查询\n"
        "4. 物流咨询 — 发货时间、运费、配送时效\n"
        "5. 尺码推荐 — 根据客户身材推荐合适尺码\n\n"
        "【回答规则】\n"
        "1. 当用户询问「你是谁」「你叫什么」等身份问题时，介绍你是客服助手「牛马」及你的职责\n"
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
        "知识库检索结果：\n{context}"
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

    def _call_api(self, messages, stream=False, max_tokens=1024):
        """调用 LLM API 的公共方法。"""
        try:
            response = requests.post(
                f"{self.BASE_URL}/chat/completions",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                json={
                    "model": self.MODEL,
                    "messages": messages,
                    "stream": stream,
                    "max_tokens": max_tokens,
                },
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
