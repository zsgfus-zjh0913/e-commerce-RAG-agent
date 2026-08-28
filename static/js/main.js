// ═══ DOM 引用 ═══
const chatMessages = document.getElementById("chat-messages");
const chatInput = document.getElementById("chat-input");
const sendBtn = document.getElementById("send-btn");
const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("file-input");
const uploadStatus = document.getElementById("upload-status");
const docList = document.getElementById("doc-list");
const docCount = document.getElementById("doc-count");
const statDocs = document.getElementById("stat-docs");
const statChunks = document.getElementById("stat-chunks");
const apiKeyInput = document.getElementById("api-key-input");
const saveKeyBtn = document.getElementById("save-key-btn");
const llmStatus = document.getElementById("llm-status");
const sidebar = document.getElementById("sidebar");
const sidebarToggle = document.getElementById("sidebar-toggle");
const sidebarClose = document.getElementById("sidebar-close");
const sidebarOverlay = document.getElementById("sidebar-overlay");
const historyList = document.getElementById("history-list");
const userAvatar = document.getElementById("user-avatar");
const userName = document.getElementById("user-name");

// ═══ 全局状态 ═══
let sessionId = null;
let conversationId = null;
let userGender = "male";
let currentMessages = [];

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
    sidebar.classList.add("open");
    sidebarOverlay.classList.add("active");
}

function closeSidebar() {
    sidebar.classList.remove("open");
    sidebarOverlay.classList.remove("active");
}

sidebarToggle.addEventListener("click", () => {
    if (window.innerWidth <= 768) {
        openSidebar();
    } else {
        sidebar.classList.toggle("collapsed");
    }
});

sidebarClose.addEventListener("click", () => {
    if (window.innerWidth <= 768) {
        closeSidebar();
    } else {
        sidebar.classList.add("collapsed");
    }
});

sidebarOverlay.addEventListener("click", closeSidebar);

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
        llmStatus.className = "llm-status connected";
        llmStatus.querySelector(".llm-text").textContent = "已连接";
    } else {
        llmStatus.className = "llm-status disconnected";
        llmStatus.querySelector(".llm-text").textContent = "未连接 · 离线RAG";
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
            <span class="history-icon">💬</span>
            <span class="history-title">${escapeHtml(conv.title)}</span>
            <button class="history-delete" onclick="deleteConversation(event, '${conv.id}')" title="删除">✕</button>
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
    showWelcomeMessage();
    loadHistory();
    chatInput.focus();
    if (window.innerWidth <= 768) closeSidebar();
}

function showWelcomeMessage() {
    chatMessages.innerHTML = `
        <div class="message bot-message">
            <div class="message-avatar">🐂</div>
            <div class="message-content">
                <div class="message-text">
                    您好，我是客服助手<strong>牛马</strong>。我可以帮您：
                    <br><br>
                    <strong>商品信息</strong> — 查询商品详情、价格、尺码、库存<br>
                    <strong>更换商品</strong> — 换货政策、换码流程<br>
                    <strong>退换货</strong> — 退货政策、退款进度<br>
                    <strong>物流咨询</strong> — 发货时间、运费、配送时效
                    <br><br>
                    <span class="example-hint">试试以下问题：</span>
                </div>
                <div class="example-questions">
                    <button class="example-q" onclick="askExample('有哪些商品？')">有哪些商品？</button>
                    <button class="example-q" onclick="askExample('服装尺码表是怎样的？')">服装尺码表</button>
                    <button class="example-q" onclick="askExample('退换货政策是什么？')">退换货政策</button>
                    <button class="example-q" onclick="askExample('蓝牙耳机的参数？')">蓝牙耳机参数</button>
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
    avatar.className = "message-avatar";
    avatar.textContent = role === "user" ? getUserAvatar() : "🐂";

    const content = document.createElement("div");
    content.className = "message-content";

    const textDiv = document.createElement("div");
    textDiv.className = "message-text";
    textDiv.innerHTML = renderMarkdown(text);
    content.appendChild(textDiv);

    if (sources && sources.length > 0) {
        const sourcesDiv = document.createElement("div");
        sourcesDiv.className = "message-sources";
        sourcesDiv.style.alignSelf = role === "user" ? "flex-end" : "flex-start";
        sources.forEach((s) => {
            const tag = document.createElement("span");
            tag.className = "source-tag";
            tag.innerHTML = `📎 ${escapeHtml(s.source)} <span class="source-score">${(s.score * 100).toFixed(0)}%</span>`;
            sourcesDiv.appendChild(tag);
        });
        content.appendChild(sourcesDiv);
    }

    wrapper.appendChild(avatar);
    wrapper.appendChild(content);
    chatMessages.appendChild(wrapper);
    scrollToBottom();
    return { wrapper, content, textDiv };
}

function addTypingIndicator() {
    const wrapper = document.createElement("div");
    wrapper.className = "message bot-message";
    wrapper.id = "typing-wrapper";

    const avatar = document.createElement("div");
    avatar.className = "message-avatar";
    avatar.textContent = "🐂";

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

async function sendMessage() {
    const question = chatInput.value.trim();
    if (!question) return;

    addMessage("user", question);
    chatInput.value = "";
    sendBtn.disabled = true;

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
            }
        }
    } catch (err) {
        removeTypingIndicator();
        addMessage("bot", "网络错误，请检查服务器是否正在运行。");
    }

    sendBtn.disabled = false;
    chatInput.focus();
}

async function handleSSEResponse(res) {
    const { content, textDiv } = addMessage("bot", "");
    textDiv.className = "message-text stream-cursor";
    let sources = [];
    let fullText = "";

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
                        textDiv.className = "message-text";
                        textDiv.innerHTML = renderMarkdown(fullText);
                        if (sources.length > 0) {
                            const sourcesDiv = document.createElement("div");
                            sourcesDiv.className = "message-sources";
                            sources.forEach((s) => {
                                const tag = document.createElement("span");
                                tag.className = "source-tag";
                                tag.innerHTML = `📎 ${escapeHtml(s.source)} <span class="source-score">${(s.score * 100).toFixed(0)}%</span>`;
                                sourcesDiv.appendChild(tag);
                            });
                            content.appendChild(sourcesDiv);
                        }
                        scrollToBottom();
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

chatInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
});

sendBtn.addEventListener("click", sendMessage);

// ═══ 文件上传 ═══
dropzone.addEventListener("click", () => fileInput.click());

dropzone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropzone.classList.add("dragover");
});

dropzone.addEventListener("dragleave", () => {
    dropzone.classList.remove("dragover");
});

dropzone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropzone.classList.remove("dragover");
    handleFiles(e.dataTransfer.files);
});

fileInput.addEventListener("change", (e) => {
    handleFiles(e.target.files);
    fileInput.value = "";
});

const SUPPORTED_EXTS = [".txt", ".md", ".csv", ".json", ".docx", ".pdf", ".xlsx", ".xls", ".pptx", ".html", ".htm"];

async function handleFiles(files) {
    for (const file of files) {
        const ext = "." + file.name.split(".").pop().toLowerCase();
        if (!SUPPORTED_EXTS.includes(ext)) {
            showUploadStatus(`不支持的文件类型: ${file.name}（${ext}）`, "error");
            continue;
        }
        const formData = new FormData();
        formData.append("file", file);
        showUploadStatus(`正在上传 ${file.name}...`, "");
        try {
            const res = await fetch("/api/upload", { method: "POST", body: formData });
            if (res.status === 401) { window.location.href = "/login"; return; }
            const data = await res.json();
            if (data.success) {
                showUploadStatus(data.message, "success");
                loadDocuments();
                loadStats();
            } else {
                showUploadStatus(data.error || "上传失败", "error");
            }
        } catch (err) {
            showUploadStatus("上传失败: " + err.message, "error");
        }
    }
}

function showUploadStatus(msg, type) {
    uploadStatus.textContent = msg;
    uploadStatus.className = "upload-status " + type;
    if (type === "success") {
        setTimeout(() => {
            uploadStatus.textContent = "";
            uploadStatus.className = "upload-status";
        }, 3000);
    }
}

// ═══ 文档列表 ═══
async function loadDocuments() {
    try {
        const res = await fetch("/api/documents");
        if (res.status === 401) { window.location.href = "/login"; return; }
        const data = await res.json();
        renderDocList(data.documents);
    } catch (err) {
        console.error("加载文档列表失败:", err);
    }
}

function renderDocList(docs) {
    docCount.textContent = docs.length;
    if (docs.length === 0) {
        docList.innerHTML = '<div class="doc-empty">暂无文档，请上传</div>';
        return;
    }
    docList.innerHTML = "";
    docs.forEach((doc) => {
        const item = document.createElement("div");
        item.className = "doc-item";
        item.innerHTML = `
            <span class="doc-icon">${getFileIcon(doc.name)}</span>
            <div class="doc-info">
                <div class="doc-name" title="${escapeHtml(doc.name)}">${escapeHtml(doc.name)}</div>
                <div class="doc-size">${formatSize(doc.size)}</div>
            </div>
            <button class="doc-delete" title="删除" onclick="deleteDoc('${escapeHtml(doc.name)}')">✕</button>
        `;
        docList.appendChild(item);
    });
}

async function deleteDoc(filename) {
    if (!confirm(`确定删除文件 "${filename}" 吗？`)) return;
    try {
        const res = await fetch("/api/delete", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ filename: filename }),
        });
        const data = await res.json();
        if (data.success) {
            showUploadStatus(data.message, "success");
            loadDocuments();
            loadStats();
        } else {
            showUploadStatus(data.error || "删除失败", "error");
        }
    } catch (err) {
        showUploadStatus("删除失败: " + err.message, "error");
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

// ═══ 初始化 ═══
(async function init() {
    await loadUserInfo();
    await initSession();
    await loadSettings();
    await loadHistory();
    await loadDocuments();
    await loadStats();
    chatInput.focus();
})();
