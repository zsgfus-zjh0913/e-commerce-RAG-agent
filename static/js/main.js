// ═══ DOM 引用 ═══
const chatMessages = document.getElementById("chat-messages");
const chatInput = document.getElementById("chat-input");
const sendBtn = document.getElementById("send-btn");
const statDocs = document.getElementById("stat-docs");
const statChunks = document.getElementById("stat-chunks");
const apiKeyInput = document.getElementById("api-key-input");
const saveKeyBtn = document.getElementById("save-key-btn");
const llmStatus = document.getElementById("llm-status");
const sidebar = document.getElementById("sidebar");
const sidebarToggleBtn = document.getElementById("sidebar-toggle");
const sidebarExpand = document.getElementById("sidebar-expand");
const sidebarOverlay = document.getElementById("sidebar-overlay");
const historyList = document.getElementById("history-list");
const ticketList = document.getElementById("ticket-list");
const ticketCount = document.getElementById("ticket-count");
const userAvatar = document.getElementById("user-avatar");
const userName = document.getElementById("user-name");
const apiKeyToggle = document.getElementById("api-key-toggle");
const apiKeyDropdown = document.getElementById("api-key-dropdown");
const imageUploadBtn = document.getElementById("image-upload-btn");
const imageInput = document.getElementById("image-input");
const imagePreview = document.getElementById("image-preview");
const imagePreviewImg = document.getElementById("image-preview-img");
const imagePreviewName = document.getElementById("image-preview-name");
const imageRemoveBtn = document.getElementById("image-remove-btn");

// ═══ 全局状态 ═══
let sessionId = null;
let conversationId = null;
let userGender = "male";
let currentMessages = [];
let humanMode = false;
let activeTicketId = null;
let pollTimer = null;
let lastMsgId = 0;
let lastTicketStatus = null;
let autoResumeAttempted = false;
let inactivityTimer = null;
let lastQuestion = "";
let lastBotMessage = "";
let ratingShown = false;
let farewellShown = false;
let pendingImageFile = null;
let pendingImageUrl = null;

// ═══ Markdown 渲染 ═══
if (typeof marked !== "undefined") {
    marked.setOptions({ breaks: true, gfm: true });
}

function renderMarkdown(text) {
    if (typeof marked !== "undefined" && typeof DOMPurify !== "undefined") {
        const html = marked.parse(text);
        return DOMPurify.sanitize(html);
    }
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML.replace(/\n/g, "<br>");
}

function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
}

function scrollToBottom() {
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

function formatSize(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / 1048576).toFixed(1) + " MB";
}

function getFileIcon(filename) {
    const ext = filename.split(".").pop().toLowerCase();
    const icons = {
        txt: "📄", md: "📝", csv: "📊", json: "📋", docx: "📘",
        pdf: "📕", xlsx: "📗", xls: "📗", pptx: "📙", html: "📄", htm: "📄",
    };
    return icons[ext] || "📄";
}

function getUserAvatar() {
    return userGender === "female" ? "👧" : "👦";
}

// ═══ 侧边栏控制（可收起） ═══
function openSidebar() {
    sidebar.classList.add("show");
    sidebarOverlay.classList.add("active");
}

function closeSidebar() {
    sidebar.classList.remove("show");
    sidebarOverlay.classList.remove("active");
}

sidebarToggleBtn.addEventListener("click", () => {
    if (window.innerWidth <= 768) {
        closeSidebar();
    } else {
        sidebar.classList.add("collapsed");
    }
});

sidebarExpand.addEventListener("click", () => {
    if (window.innerWidth <= 768) {
        openSidebar();
    } else {
        sidebar.classList.remove("collapsed");
    }
});

sidebarOverlay.addEventListener("click", closeSidebar);

// ═══ API Key 面板 ═══
apiKeyToggle.addEventListener("click", (e) => {
    e.stopPropagation();
    apiKeyDropdown.classList.toggle("show");
});

document.addEventListener("click", (e) => {
    if (!apiKeyDropdown.contains(e.target) && !apiKeyToggle.contains(e.target)) {
        apiKeyDropdown.classList.remove("show");
    }
});

apiKeyDropdown.addEventListener("click", (e) => {
    e.stopPropagation();
});

// ═══ 用户信息加载 ═══
async function loadUserInfo() {
    try {
        const res = await fetch("/api/user");
        if (res.status === 401) {
            window.location.href = "/login";
            return;
        }
        const data = await res.json();
        userName.textContent = data.username;
        userGender = data.gender || "male";
        userAvatar.textContent = getUserAvatar();
    } catch (err) {
        console.error("加载用户信息失败:", err);
    }
}

// ═══ 退出登录 ═══
async function logout() {
    try {
        await fetch("/api/logout", { method: "POST" });
    } catch (err) {
        console.error("退出失败:", err);
    }
    window.location.href = "/login";
}

// ═══ Session 管理 ═══
async function initSession() {
    try {
        const res = await fetch("/api/session", { method: "GET" });
        if (res.status === 401) {
            window.location.href = "/login";
            return;
        }
        const data = await res.json();
        sessionId = data.session_id;
    } catch (err) {
        console.error("Session 初始化失败:", err);
        sessionId = "sess_" + Date.now() + "_" + Math.random().toString(36).slice(2, 8);
    }
}

// ═══ API Key 管理 ═══
function updateLlmStatus(enabled) {
    if (enabled) {
        llmStatus.className = "llm-status-badge connected";
        llmStatus.querySelector(".llm-text").textContent = "在线模式";
    } else {
        llmStatus.className = "llm-status-badge disconnected";
        llmStatus.querySelector(".llm-text").textContent = "离线模式";
    }
}

async function loadSettings() {
    try {
        const res = await fetch("/api/settings");
        if (res.status === 401) { window.location.href = "/login"; return; }
        const data = await res.json();
        updateLlmStatus(data.llm_enabled);
    } catch (err) {
        console.error("加载设置失败:", err);
    }
}

async function saveApiKey() {
    const key = apiKeyInput.value.trim();
    try {
        const res = await fetch("/api/settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ api_key: key }),
        });
        const data = await res.json();
        if (data.success) {
            updateLlmStatus(data.llm_enabled);
            if (key) {
                apiKeyInput.value = "";
            }
            addMessage("bot", data.message);
        }
    } catch (err) {
        addMessage("bot", "保存设置失败: " + err.message);
    }
}

saveKeyBtn.addEventListener("click", saveApiKey);
apiKeyInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") saveApiKey();
});

// ═══ 历史记录管理 ═══
async function loadHistory() {
    try {
        const res = await fetch("/api/history");
        if (res.status === 401) { window.location.href = "/login"; return; }
        const data = await res.json();
        renderHistoryList(data.conversations);
    } catch (err) {
        console.error("加载历史失败:", err);
    }
}

function renderHistoryList(convs) {
    if (!convs || convs.length === 0) {
        historyList.innerHTML = '<div class="history-empty">暂无历史对话</div>';
        return;
    }
    historyList.innerHTML = "";
    convs.forEach((conv) => {
        const item = document.createElement("div");
        item.className = "history-item" + (conv.id === conversationId ? " active" : "");
        item.onclick = () => loadConversation(conv.id);
        item.innerHTML = `
            <span class="history-item-title">${escapeHtml(conv.title)}</span>
            <button class="history-item-delete" onclick="deleteConversation(event, '${conv.id}')" title="删除">✕</button>
        `;
        historyList.appendChild(item);
    });
}

async function loadConversation(convId) {
    try {
        const res = await fetch(`/api/history/${convId}`);
        if (res.status === 401) { window.location.href = "/login"; return; }
        const conv = await res.json();
        if (conv.error) return;

        conversationId = conv.id;
        chatMessages.innerHTML = "";

        if (!conv.messages || conv.messages.length === 0) {
            showWelcomeMessage();
        } else {
            conv.messages.forEach((msg) => {
                addMessage(msg.role, msg.content);
            });
        }
        loadHistory();
        scrollToBottom();
    } catch (err) {
        console.error("加载对话失败:", err);
    }
}

async function deleteConversation(e, convId) {
    e.stopPropagation();
    if (!confirm("确定删除这段对话吗？")) return;
    try {
        const res = await fetch(`/api/history/${convId}`, { method: "DELETE" });
        const data = await res.json();
        if (data.success) {
            if (convId === conversationId) {
                conversationId = null;
                showWelcomeMessage();
            }
            loadHistory();
        }
    } catch (err) {
        console.error("删除对话失败:", err);
    }
}

function newConversation() {
    conversationId = null;
    currentMessages = [];
    ratingShown = false;
    farewellShown = false;
    stopInactivityTimer();
    showWelcomeMessage();
    loadHistory();
    chatInput.focus();
    if (window.innerWidth <= 768) closeSidebar();
}

function showWelcomeMessage() {
    chatMessages.innerHTML = `
        <div class="message bot-message">
            <div class="message-avatar bot-avatar">🌸</div>
            <div class="message-content">
                <div class="message-text">
                    您好，我是客服助手<strong>暖小助</strong>。我可以帮您：
                    <br><br>
                    <strong>商品信息</strong> — 查询商品详情、价格、尺码、库存<br>
                    <strong>更换商品</strong> — 换货政策、换码流程<br>
                    <strong>退换货</strong> — 退货政策、退款进度<br>
                    <strong>物流咨询</strong> — 发货时间、运费、配送时效<br>
                    <strong>转人工</strong> — 说"转人工"即可接入人工客服
                    <br><br>
                    <span class="example-hint">试试以下问题：</span>
                </div>
                <div class="example-questions">
                    <button class="example-q" onclick="askExample('有哪些商品？')">有哪些商品？</button>
                    <button class="example-q" onclick="askExample('服装尺码表是怎样的？')">服装尺码表</button>
                    <button class="example-q" onclick="askExample('退换货政策是什么？')">退换货政策</button>
                    <button class="example-q" onclick="askExample('蓝牙耳机的参数？')">蓝牙耳机参数</button>
                    <button class="example-q" onclick="askExample('我要退款')">申请退款</button>
                </div>
            </div>
        </div>
    `;
}

// ═══ 聊天功能 ═══
function addMessage(role, text, sources) {
    const wrapper = document.createElement("div");
    wrapper.className = `message ${role}-message`;

    const avatar = document.createElement("div");
    avatar.className = "message-avatar " + (role === "user" ? "user-avatar-msg" : "bot-avatar");
    avatar.textContent = role === "user" ? getUserAvatar() : "🌸";

    const content = document.createElement("div");
    content.className = "message-content";

    const textDiv = document.createElement("div");
    textDiv.className = "message-text";
    textDiv.innerHTML = renderMarkdown(text);
    content.appendChild(textDiv);

    wrapper.appendChild(avatar);
    wrapper.appendChild(content);
    chatMessages.appendChild(wrapper);
    scrollToBottom();

    if (role === "bot") {
        lastBotMessage = text;
    }

    return { wrapper, content, textDiv };
}

function addTypingIndicator() {
    const wrapper = document.createElement("div");
    wrapper.className = "message bot-message";
    wrapper.id = "typing-wrapper";

    const avatar = document.createElement("div");
    avatar.className = "message-avatar bot-avatar";
    avatar.textContent = "🌸";

    const indicator = document.createElement("div");
    indicator.className = "typing-indicator";
    indicator.innerHTML = "<span></span><span></span><span></span>";

    wrapper.appendChild(avatar);
    wrapper.appendChild(indicator);
    chatMessages.appendChild(wrapper);
    scrollToBottom();
}

function removeTypingIndicator() {
    const el = document.getElementById("typing-wrapper");
    if (el) el.remove();
}

function clearPendingImage() {
    if (pendingImageUrl) {
        URL.revokeObjectURL(pendingImageUrl);
    }
    pendingImageFile = null;
    pendingImageUrl = null;
    imageInput.value = "";
    imagePreview.style.display = "none";
    imagePreviewImg.removeAttribute("src");
    imagePreviewName.textContent = "";
}

function setPendingImage(file) {
    if (!file) return;
    if (!file.type.startsWith("image/")) {
        addMessage("bot", "请选择 JPG、PNG、WebP 或 BMP 图片。");
        return;
    }
    if (file.size > 10 * 1024 * 1024) {
        addMessage("bot", "图片不能超过 10MB。");
        return;
    }

    clearPendingImage();
    pendingImageFile = file;
    pendingImageUrl = URL.createObjectURL(file);
    imagePreviewImg.src = pendingImageUrl;
    imagePreviewName.textContent = file.name;
    imagePreview.style.display = "flex";
    chatInput.focus();
}

function addImageMessage(file, text) {
    const wrapper = document.createElement("div");
    wrapper.className = "message user-message";

    const avatar = document.createElement("div");
    avatar.className = "message-avatar user-avatar-msg";
    avatar.textContent = getUserAvatar();

    const content = document.createElement("div");
    content.className = "message-content";

    const image = document.createElement("img");
    image.className = "message-image";
    image.src = URL.createObjectURL(file);
    image.alt = "用户上传的商品图片";
    image.onload = () => {
        const objectUrl = image.src;
        setTimeout(() => URL.revokeObjectURL(objectUrl), 60000);
    };
    content.appendChild(image);

    if (text) {
        const textDiv = document.createElement("div");
        textDiv.className = "message-text";
        textDiv.innerHTML = renderMarkdown(text);
        content.appendChild(textDiv);
    }

    wrapper.appendChild(avatar);
    wrapper.appendChild(content);
    chatMessages.appendChild(wrapper);
    scrollToBottom();
}

async function sendImageMessage(question) {
    const file = pendingImageFile;
    if (!file) return;

    addImageMessage(file, question);
    clearPendingImage();
    sendBtn.disabled = true;
    stopInactivityTimer();

    const formData = new FormData();
    formData.append("image", file);
    formData.append("question", question || "");
    formData.append("session_id", sessionId || "");
    formData.append("conversation_id", conversationId || "");

    try {
        const res = await fetch("/api/query/image", {
            method: "POST",
            body: formData,
        });
        if (res.status === 401) {
            window.location.href = "/login";
            return;
        }
        if (!res.ok) {
            let errorText = "图片处理失败，请稍后重试。";
            try {
                const errorData = await res.json();
                errorText = errorData.error || errorText;
            } catch (err) {
                // 保留通用错误提示
            }
            addMessage("bot", errorText);
            resetInactivityTimer();
            return;
        }

        const contentType = res.headers.get("content-type") || "";
        if (contentType.includes("text/event-stream")) {
            await handleSSEResponse(res);
        } else {
            const data = await res.json();
            addMessage("bot", data.error || "图片处理失败，请稍后重试。");
            resetInactivityTimer();
        }
    } catch (err) {
        addMessage("bot", "图片上传失败，请检查服务器是否正在运行。");
        resetInactivityTimer();
    } finally {
        sendBtn.disabled = false;
        chatInput.focus();
    }
}

async function sendMessage() {
    const question = chatInput.value.trim();
    if (pendingImageFile) {
        chatInput.value = "";
        await sendImageMessage(question);
        return;
    }
    if (!question) return;

    addMessage("user", question);
    chatInput.value = "";
    sendBtn.disabled = true;
    lastQuestion = question;
    ratingShown = false;
    stopInactivityTimer();

    // 人工客服模式：消息发送给坐席
    if (humanMode && activeTicketId) {
        try {
            const res = await fetch("/api/chat/send", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    ticket_id: activeTicketId,
                    message: question,
                }),
            });
            if (res.status === 401) { window.location.href = "/login"; return; }
            const data = await res.json();
            if (!data.success) {
                addMessage("bot", "发送失败: " + (data.error || ""));
            }
        } catch (err) {
            addMessage("bot", "网络错误，请检查服务器是否正在运行。");
        }
        sendBtn.disabled = false;
        chatInput.focus();
        return;
    }

    try {
        const res = await fetch("/api/query", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                question: question,
                top_k: 5,
                session_id: sessionId || "",
                conversation_id: conversationId || "",
            }),
        });

        if (res.status === 401) {
            window.location.href = "/login";
            return;
        }

        const contentType = res.headers.get("content-type") || "";
        if (contentType.includes("text/event-stream")) {
            await handleSSEResponse(res);
        } else {
            addTypingIndicator();
            const data = await res.json();
            removeTypingIndicator();
            if (data.error) {
                addMessage("bot", "出错了：" + data.error);
            } else {
                addMessage("bot", data.answer, data.sources);
                if (isAcknowledgment(lastQuestion)) {
                    setTimeout(showRatingWidget, 500);
                }
            }
            resetInactivityTimer();
        }
    } catch (err) {
        removeTypingIndicator();
        addMessage("bot", "网络错误，请检查服务器是否正在运行。");
        resetInactivityTimer();
    }

    sendBtn.disabled = false;
    chatInput.focus();
}

async function handleSSEResponse(res) {
    const { content, textDiv } = addMessage("bot", "");
    textDiv.className = "message-text stream-cursor";
    let sources = [];
    let fullText = "";
    let multimodalInfo = null;

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buffer.indexOf("\n\n")) !== -1) {
            const rawEvent = buffer.slice(0, idx);
            buffer = buffer.slice(idx + 2);

            const lines = rawEvent.split("\n");
            for (const line of lines) {
                if (!line.startsWith("data: ")) continue;
                const jsonStr = line.slice(6);
                try {
                    const evt = JSON.parse(jsonStr);

                    if (evt.type === "rewrite") {
                        // 改写查询内部使用，不展示给用户
                    } else if (evt.type === "rewrite_error") {
                        // 改写失败静默处理，不影响回答
                    } else if (evt.type === "ticket") {
                        // 工单创建成功，刷新工单列表
                        if (evt.data) {
                            loadTickets();
                            // 转人工、退款、换货、售后工单均进入人工会话
                            if (isLiveTicket(evt.data)) {
                                activeTicketId = evt.data.ticket_id;
                                lastTicketStatus = evt.data.status;
                                humanMode = true;
                                rememberHumanTicket(evt.data.ticket_id);
                                startAgentPolling();
                                stopInactivityTimer();
                            }
                        }
                    } else if (evt.type === "refund_card") {
                        renderRefundCard(evt.data.orders);
                    } else if (evt.type === "multimodal") {
                        // 图片检索诊断：OCR 文字 / 向量提供者
                        multimodalInfo = evt.data || {};
                    } else if (evt.type === "sources") {
                        sources = evt.data || [];
                    } else if (evt.type === "delta") {
                        fullText += evt.data;
                        textDiv.innerHTML = renderMarkdown(fullText);
                        scrollToBottom();
                    } else if (evt.type === "conversation_id") {
                        conversationId = evt.data;
                        loadHistory();
                    } else if (evt.type === "done") {
                        // 如果有 OCR 识别结果，追加提示
                        if (multimodalInfo && multimodalInfo.ocr_text) {
                            const ocr = multimodalInfo.ocr_text.trim();
                            if (ocr && !fullText.includes(ocr)) {
                                fullText += `\n\n---\n📷 图片识别文字：${ocr}`;
                            }
                        }
                        textDiv.className = "message-text";
                        textDiv.innerHTML = renderMarkdown(fullText);
                        scrollToBottom();
                        lastBotMessage = fullText;
                        if (isAcknowledgment(lastQuestion)) {
                            setTimeout(showRatingWidget, 500);
                        }
                        resetInactivityTimer();
                    }
                } catch (e) {
                    // 忽略解析错误
                }
            }
        }
    }

    textDiv.className = "message-text";
    if (fullText) {
        textDiv.innerHTML = renderMarkdown(fullText);
    }
}

function askExample(question) {
    chatInput.value = question;
    sendMessage();
}

imageUploadBtn.addEventListener("click", () => imageInput.click());
imageInput.addEventListener("change", () => {
    setPendingImage(imageInput.files && imageInput.files[0]);
});
imageRemoveBtn.addEventListener("click", clearPendingImage);

chatInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
});

// 输入框自动增高
chatInput.addEventListener("input", () => {
    chatInput.style.height = "auto";
    chatInput.style.height = Math.min(chatInput.scrollHeight, 100) + "px";
});

sendBtn.addEventListener("click", sendMessage);

// ═══ 确认词检测与星级评价 ═══
function isAcknowledgment(text) {
    const ackPatterns = [
        "好的", "好嘞", "好滴", "好吧", "好", "行", "可以", "收到",
        "明白", "了解", "知道了", "懂了", "嗯", "嗯嗯", "嗯好",
        "ok", "OK", "Ok", "oK", "okay", "fine",
    ];
    const trimmed = text.trim().toLowerCase();
    if (!ackPatterns.some(p => trimmed === p.toLowerCase())) return false;

    // 如果上一条机器人消息是提问或建议（如"要不要查下库存？"），
    // 则"好的"是确认动作，不是简单结束语，不触发评价
    if (lastBotMessage) {
        const actionPatterns = [
            "要不要", "是否需要", "需要我", "帮您", "要不要查", "要不要看",
            "可以帮", "需要帮", "是否帮", "能否帮", "想不想",
        ];
        if (actionPatterns.some(p => lastBotMessage.includes(p))) {
            return false;
        }
        if (/[？?]\s*$/.test(lastBotMessage.trim())) {
            return false;
        }
    }
    return true;
}

function showRatingWidget() {
    if (ratingShown) return;
    ratingShown = true;

    const wrapper = document.createElement("div");
    wrapper.className = "message bot-message";

    const avatar = document.createElement("div");
    avatar.className = "message-avatar bot-avatar";
    avatar.textContent = "🌸";

    const content = document.createElement("div");
    content.className = "message-content";

    const textDiv = document.createElement("div");
    textDiv.className = "message-text";
    textDiv.innerHTML = "请问您对本次服务还满意吗？可以给暖小助评个分吗？⬇️";

    const ratingDiv = document.createElement("div");
    ratingDiv.className = "rating-widget";
    ratingDiv.innerHTML = `
        <span class="rating-star" data-val="1">★</span>
        <span class="rating-star" data-val="2">★</span>
        <span class="rating-star" data-val="3">★</span>
        <span class="rating-star" data-val="4">★</span>
        <span class="rating-star" data-val="5">★</span>
    `;

    ratingDiv.querySelectorAll(".rating-star").forEach(star => {
        star.addEventListener("mouseenter", () => {
            const val = parseInt(star.dataset.val);
            ratingDiv.querySelectorAll(".rating-star").forEach((s, i) => {
                s.classList.toggle("hover", i < val);
            });
        });
        star.addEventListener("mouseleave", () => {
            ratingDiv.querySelectorAll(".rating-star").forEach(s => {
                s.classList.remove("hover");
            });
        });
        star.addEventListener("click", () => {
            const val = parseInt(star.dataset.val);
            submitRating(val, ratingDiv);
        });
    });

    content.appendChild(textDiv);
    content.appendChild(ratingDiv);
    wrapper.appendChild(avatar);
    wrapper.appendChild(content);
    chatMessages.appendChild(wrapper);
    scrollToBottom();
}

async function submitRating(val, container) {
    container.querySelectorAll(".rating-star").forEach((s, i) => {
        s.classList.toggle("active", i < val);
                s.classList.remove("hover");
    });
    container.style.pointerEvents = "none";
    const thankYou = document.createElement("div");
    thankYou.className = "rating-thankyou";
    thankYou.textContent = "感谢您的评价！祝您生活愉快~ 🌸";
    container.appendChild(thankYou);
    scrollToBottom();

    try {
        await fetch("/api/rating", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                rating: val,
                conversation_id: conversationId || "",
                question: lastQuestion,
            }),
        });
    } catch (err) {
        // 评价提交失败静默处理
    }
}

// ═══ 2分钟无回复自动提醒 ═══
function resetInactivityTimer() {
    farewellShown = false;
    if (inactivityTimer) clearTimeout(inactivityTimer);
    inactivityTimer = setTimeout(() => {
        if (humanMode) return;
        if (farewellShown) return;
        farewellShown = true;
        addMessage("bot", "亲亲，这边看您暂时没有回复，这边先不打扰您了，如果您有后续有任何疑虑欢迎随时咨询~您也可以添加我们的客服微信进一步咨询哦~");
    }, 120000);
}

function stopInactivityTimer() {
    if (inactivityTimer) {
        clearTimeout(inactivityTimer);
        inactivityTimer = null;
    }
}

// ═══ 统计信息 ═══
async function loadStats() {
    try {
        const res = await fetch("/api/stats");
        if (res.status === 401) { window.location.href = "/login"; return; }
        const data = await res.json();
        statDocs.textContent = data.total_documents || 0;
        statChunks.textContent = data.total_chunks || 0;
    } catch (err) {
        console.error("加载统计失败:", err);
    }
}

// ═══ 人工会话恢复 ═══
function isLiveTicket(ticket) {
    return ["human_agent", "refund", "exchange", "after_sale"].includes(ticket.ticket_type)
        && ["pending", "approved"].includes(ticket.status);
}

const ACTIVE_HUMAN_TICKET_KEY = "activeHumanTicketId";

function rememberHumanTicket(ticketId) {
    try {
        sessionStorage.setItem(ACTIVE_HUMAN_TICKET_KEY, ticketId);
    } catch (err) {
        // 浏览器禁用 sessionStorage 时仅保留当前页面会话
    }
}

function forgetHumanTicket() {
    try {
        sessionStorage.removeItem(ACTIVE_HUMAN_TICKET_KEY);
    } catch (err) {
        // 忽略存储异常
    }
}

function getRememberedHumanTicket() {
    try {
        return sessionStorage.getItem(ACTIVE_HUMAN_TICKET_KEY);
    } catch (err) {
        return null;
    }
}

function stopHumanSessionPolling() {
    if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
    }
}

async function loadHumanHistory(ticketId) {
    const res = await fetch(`/api/ticket/${ticketId}/history`);
    if (res.status === 401) {
        window.location.href = "/login";
        return null;
    }
    const data = await res.json();
    if (data.error) throw new Error(data.error);

    chatMessages.innerHTML = "";
    lastMsgId = 0;
    (data.messages || []).forEach((message) => {
        if (message.sender === "user") {
            addMessage("user", message.message);
        } else {
            addAgentMessage(message.message);
        }
        if (message.sender === "agent" && message.id > lastMsgId) {
            lastMsgId = message.id;
        }
    });
    if (!data.messages || data.messages.length === 0) {
        addMessage("bot", `已进入人工服务会话（${ticketId}）。请描述您的问题，等待坐席接入。`);
    }
    return data.ticket;
}

async function resumeHumanSession(ticket) {
    if (!ticket || !isLiveTicket(ticket)) return;
    rememberHumanTicket(ticket.ticket_id);
    humanMode = true;
    activeTicketId = ticket.ticket_id;
    lastTicketStatus = ticket.status;
    stopHumanSessionPolling();
    stopInactivityTimer();
    try {
        const currentTicket = await loadHumanHistory(ticket.ticket_id);
        if (currentTicket) {
            lastTicketStatus = currentTicket.status;
        }
        startAgentPolling();
        loadTickets();
    } catch (err) {
        console.error("恢复人工会话失败:", err);
    }
}

// ═══ 工单管理 ═══
async function loadTickets() {
    try {
        const res = await fetch("/api/ticket/list");
        if (res.status === 401) { window.location.href = "/login"; return; }
        const data = await res.json();
        renderTicketList(data.tickets || []);
    } catch (err) {
        console.error("加载工单失败:", err);
    }
}

function renderTicketList(tickets) {
    ticketCount.textContent = tickets.length;
    if (tickets.length === 0) {
        ticketList.innerHTML = '<div class="ticket-empty">暂无工单</div>';
        return;
    }
    ticketList.innerHTML = "";
    tickets.forEach((t) => {
        const statusText = {
            pending: "待处理",
            approved: "已确认",
            rejected: "已拒绝",
            resolved: "已完成",
        }[t.status] || t.status;
        const typeText = {
            refund: "退款",
            human_agent: "转人工",
            exchange: "换货",
            after_sale: "售后",
        }[t.ticket_type] || t.ticket_type;
        const item = document.createElement("div");
        item.className = "ticket-card" + (t.ticket_id === activeTicketId ? " active" : "");
        if (isLiveTicket(t)) {
            item.classList.add("clickable");
            item.onclick = () => resumeHumanSession(t);
        }
        item.innerHTML = `
            <div class="ticket-card-header">
                <span class="ticket-type-badge ${t.ticket_type}">${typeText}</span>
                <span class="ticket-status ${t.status}">${statusText}</span>
            </div>
            <div class="ticket-id">${escapeHtml(t.ticket_id)}</div>
            <div class="ticket-subject" title="${escapeHtml(t.subject)}">${escapeHtml(t.subject)}</div>
        `;
        ticketList.appendChild(item);
    });

    const rememberedTicketId = getRememberedHumanTicket();
    const rememberedTicket = rememberedTicketId
        ? tickets.find((t) => t.ticket_id === rememberedTicketId && isLiveTicket(t))
        : null;
    if (!humanMode && rememberedTicket && !autoResumeAttempted) {
        autoResumeAttempted = true;
        resumeHumanSession(rememberedTicket);
    } else if (rememberedTicketId && !rememberedTicket) {
        forgetHumanTicket();
    }
}

// ═══ 退款卡片 ═══
function renderRefundCard(orders) {
    const wrapper = document.createElement("div");
    wrapper.className = "message bot-message";

    const avatar = document.createElement("div");
    avatar.className = "message-avatar bot-avatar";
    avatar.textContent = "🌸";

    const content = document.createElement("div");
    content.className = "message-content";

    const card = document.createElement("div");
    card.className = "refund-card";

    const title = document.createElement("div");
    title.className = "refund-card-title";
    title.textContent = "退款申请";
    card.appendChild(title);

    orders.forEach((o) => {
        if (o.status === "refunded" || o.status === "cancelled") return;
        const item = document.createElement("div");
        item.className = "refund-order-item";
        item.innerHTML = `
            <div class="refund-order-info">
                <div class="refund-order-row"><span class="refund-label">订单号</span><span class="refund-value">${escapeHtml(o.order_id)}</span></div>
                <div class="refund-order-row"><span class="refund-label">商品</span><span class="refund-value">${escapeHtml(o.product_name)}</span></div>
                <div class="refund-order-row"><span class="refund-label">数量</span><span class="refund-value">${o.quantity}</span></div>
                <div class="refund-order-row"><span class="refund-label">金额</span><span class="refund-value">${o.amount}元</span></div>
                <div class="refund-order-row"><span class="refund-label">状态</span><span class="refund-value">${escapeHtml(o.status_text || o.status)}</span></div>
            </div>
            <button class="refund-confirm-btn" onclick="confirmRefund('${o.order_id}')">确认退款</button>
        `;
        card.appendChild(item);
    });

    content.appendChild(card);
    wrapper.appendChild(avatar);
    wrapper.appendChild(content);
    chatMessages.appendChild(wrapper);
    scrollToBottom();
}

async function confirmRefund(orderId) {
    const btn = event.target;
    btn.disabled = true;
    btn.textContent = "提交中...";
    try {
        const res = await fetch("/api/refund/confirm", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ order_id: orderId }),
        });
        if (res.status === 401) { window.location.href = "/login"; return; }
        const data = await res.json();
        if (data.success) {
            btn.textContent = "已提交";
            btn.className = "refund-confirm-btn done";
            addMessage("bot", `退款申请已提交！工单号：${data.ticket.ticket_id}\n\n人工客服将尽快审核您的退款申请，审核结果会在这里通知您。`);
            loadTickets();
            resumeHumanSession(data.ticket);
        } else {
            btn.textContent = "确认退款";
            btn.disabled = false;
            addMessage("bot", "退款申请失败: " + (data.error || ""));
        }
    } catch (err) {
        btn.textContent = "确认退款";
        btn.disabled = false;
        addMessage("bot", "网络错误，请重试。");
    }
}

// ═══ 人工客服会话 ═══
function addAgentMessage(text) {
    const wrapper = document.createElement("div");
    wrapper.className = "message bot-message agent-message";

    const avatar = document.createElement("div");
    avatar.className = "message-avatar agent-avatar";
    avatar.textContent = "🧑‍💼";

    const content = document.createElement("div");
    content.className = "message-content";

    const label = document.createElement("div");
    label.className = "agent-label";
    label.textContent = "人工客服";
    content.appendChild(label);

    const textDiv = document.createElement("div");
    textDiv.className = "message-text";
    textDiv.innerHTML = renderMarkdown(text);
    content.appendChild(textDiv);

    wrapper.appendChild(avatar);
    wrapper.appendChild(content);
    chatMessages.appendChild(wrapper);
    scrollToBottom();
}

function startAgentPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(pollTicketUpdates, 3000);
}

function stopAgentPolling() {
    if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
    }
}

async function pollTicketUpdates() {
    if (!activeTicketId) return;
    try {
        const res = await fetch(`/api/ticket/${activeTicketId}/updates?after_id=${lastMsgId}`);
        if (res.status === 401) { window.location.href = "/login"; return; }
        const data = await res.json();
        if (data.error) return;

        const ticket = data.ticket;

        // 检测状态变化
        if (lastTicketStatus !== ticket.status) {
            if (ticket.status === "resolved" || ticket.status === "rejected") {
                if (!data.messages || data.messages.length === 0) {
                    addAgentMessage(ticket.agent_reply || "本次人工服务已结束。");
                }
                humanMode = false;
                activeTicketId = null;
                forgetHumanTicket();
                stopAgentPolling();
                loadTickets();
                resetInactivityTimer();
            } else if (ticket.status === "approved") {
                loadTickets();
            }
            lastTicketStatus = ticket.status;
        }

        // 显示坐席消息
        if (data.messages && data.messages.length > 0) {
            data.messages.forEach((m) => {
                addAgentMessage(m.message);
                if (m.id > lastMsgId) lastMsgId = m.id;
            });
        }
    } catch (err) {
        // 网络错误静默处理，下次重试
    }
}

// ═══ 初始化 ═══
(async function init() {
    await loadUserInfo();
    await initSession();
    await loadSettings();
    await loadHistory();
    await loadTickets();
    await loadStats();
    chatInput.focus();
})();
