(() => {
  "use strict";

  const MAX_QUESTION_LENGTH = 20000;
  const POLL_INTERVAL_MS = 2000;
  const POLL_MAX_MS = 360000;
  const state = {
    conversationId: null,
    active: null,
    messages: [],
    seenTurns: new Set(),
    pollTimer: null,
    pollDeadline: 0,
    composing: false,
  };

  const elements = {
    messages: document.getElementById("messages"),
    emptyState: document.getElementById("empty-state"),
    status: document.getElementById("status"),
    question: document.getElementById("question"),
    counter: document.getElementById("counter"),
    send: document.getElementById("send"),
    confirm: document.getElementById("confirm-request"),
    newConversation: document.getElementById("new-conversation"),
  };

  const outcomeLabels = {
    answered: "已根據文件回答",
    unable_to_answer: "目前無法可靠回答",
    external_generation_blocked: "依外送政策未產生答案",
    insufficient_evidence: "找不到足夠文件依據",
    outbound_policy_blocked: "依 AI 外送政策未產生答案",
  };

  class ApiError extends Error {
    constructor(status, payload, uncertain = false) {
      super("safe api error");
      this.status = status;
      this.payload = payload;
      this.uncertain = uncertain;
    }
  }

  function setStatus(message, isError = false) {
    elements.status.textContent = message;
    elements.status.classList.toggle("error", isError);
  }

  function setBusy(busy) {
    elements.send.disabled = busy;
    elements.newConversation.disabled = busy;
    elements.question.disabled = busy;
    elements.confirm.classList.toggle("hidden", !state.active || !state.active.uncertain);
  }

  function updateCounter() {
    elements.counter.textContent = `${[...elements.question.value].length.toLocaleString("zh-TW")} / 20,000`;
  }

  function makeCitationList(citations) {
    if (!Array.isArray(citations) || citations.length === 0) return null;
    const list = document.createElement("ul");
    list.className = "citation-list";
    for (const citation of citations) {
      const item = document.createElement("li");
      const section = citation.section_path ? `，${citation.section_path}` : "";
      const page = Number.isInteger(citation.page_number) ? `，第 ${citation.page_number} 頁` : "";
      item.textContent = `${citation.citation_id}：${citation.title}${section}${page}`;
      list.append(item);
    }
    return list;
  }

  function renderMessages() {
    elements.messages.replaceChildren();
    if (state.messages.length === 0) {
      elements.messages.append(elements.emptyState);
      return;
    }
    for (const message of state.messages) {
      const article = document.createElement("article");
      article.className = `message ${message.role}`;
      const label = document.createElement("div");
      label.className = "message-label";
      label.textContent = message.role === "user" ? "你" : (outcomeLabels[message.response_kind] || "Help Desk");
      article.append(label);

      let displayedParts = false;
      if (message.role === "assistant" && Array.isArray(message.answer_parts) && message.answer_parts.length > 0) {
        for (const part of message.answer_parts) {
          const paragraph = document.createElement("p");
          paragraph.textContent = part.text;
          article.append(paragraph);
        }
        displayedParts = true;
      }
      if (!displayedParts && message.content) {
        const paragraph = document.createElement("p");
        paragraph.textContent = message.content;
        article.append(paragraph);
      }
      const citations = makeCitationList(message.citations);
      if (citations) article.append(citations);
      elements.messages.append(article);
    }
    elements.messages.scrollTop = elements.messages.scrollHeight;
  }

  function addUserMessage(question, turnId) {
    if (state.messages.some((message) => message.turnId === turnId && message.role === "user")) return;
    state.messages.push({ role: "user", content: question, turnId, citations: [] });
    renderMessages();
  }

  function addTurnResult(payload, question) {
    if (!payload || !payload.turn_id || state.seenTurns.has(payload.turn_id)) return;
    state.seenTurns.add(payload.turn_id);
    if (payload.user_message) {
      const oldUser = state.messages.find((message) => message.turnId === payload.turn_id && message.role === "user");
      if (oldUser) {
        oldUser.content = payload.user_message.content;
      } else {
        state.messages.push({ ...payload.user_message, turnId: payload.turn_id });
      }
    } else if (question) {
      addUserMessage(question, payload.turn_id);
    }
    if (payload.assistant_message) {
      state.messages.push({ ...payload.assistant_message, turnId: payload.turn_id });
    }
    renderMessages();
  }

  function safeMessage(payload, fallback) {
    return payload && typeof payload.message === "string" ? payload.message : fallback;
  }

  async function requestJson(path, options = {}, timeoutMs = 340000) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(path, {
        ...options,
        signal: controller.signal,
        headers: { Accept: "application/json", ...(options.headers || {}) },
      });
      let payload = null;
      try {
        payload = await response.json();
      } catch (_error) {
        throw new ApiError(response.status, null, response.status >= 500);
      }
      if (!response.ok) throw new ApiError(response.status, payload, response.status >= 500);
      return { status: response.status, payload };
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw new ApiError(0, null, true);
    } finally {
      window.clearTimeout(timer);
    }
  }

  function errorText(error) {
    if (error.status === 0) return "無法確認伺服器是否收到請求，請使用「確認原請求結果」。";
    if (error.payload && typeof error.payload.message === "string") return error.payload.message;
    if (error.status === 401) return "Demo 身分未被接受。";
    if (error.status === 404) return "找不到這個對話或問題。";
    if (error.status === 409) return "目前對話狀態不允許這個操作。";
    if (error.status === 422) return "問題格式不正確。";
    if (error.status === 429) return "系統目前忙碌，請稍後再試。";
    if (error.status === 504) return "系統處理逾時，結果尚未確認。";
    return "服務暫時無法完成請求。";
  }

  function clearPolling() {
    if (state.pollTimer !== null) window.clearTimeout(state.pollTimer);
    state.pollTimer = null;
  }

  async function createConversation() {
    const result = await requestJson("/api/v1/conversations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: null }),
    }, 30000);
    if (!result.payload || typeof result.payload.conversation_id !== "string") {
      throw new ApiError(502, null, true);
    }
    return result.payload.conversation_id;
  }

  function finishActive() {
    state.active = null;
    state.pollDeadline = 0;
    clearPolling();
    setBusy(false);
  }

  function displayTerminal(payload, question) {
    addTurnResult(payload, question);
    if (payload.outcome && outcomeLabels[payload.outcome]) {
      setStatus(outcomeLabels[payload.outcome]);
    } else if (payload.state === "failed_unknown") {
      setStatus("結果尚未確認，請依安全錯誤訊息處理。", true);
    } else if (payload.error) {
      setStatus(safeMessage(payload.error, "問題處理失敗。"), true);
    }
    finishActive();
  }

  async function pollTurn() {
    const active = state.active;
    if (!active || !active.turnId) return;
    if (Date.now() >= state.pollDeadline) {
      setStatus("結果尚未確認；可以繼續查詢原請求。", true);
      state.active.uncertain = true;
      setBusy(false);
      return;
    }
    try {
      const result = await requestJson(`/api/v1/conversations/${active.conversationId}/turns/${active.turnId}`, {}, 30000);
      const payload = result.payload;
      if (payload.state === "processing") {
        setStatus("問題處理中，請稍候……");
        state.pollTimer = window.setTimeout(() => { void pollTurn(); }, POLL_INTERVAL_MS);
        return;
      }
      displayTerminal(payload, active.question);
    } catch (error) {
      if (error.uncertain) {
        setStatus("查詢結果不明；可以繼續查詢原請求。", true);
        state.active.uncertain = true;
        setBusy(false);
        return;
      }
      setStatus(errorText(error), true);
      finishActive();
    }
  }

  async function submitExisting() {
    const active = state.active;
    if (!active) return;
    state.active.uncertain = false;
    setBusy(true);
    setStatus("正在確認原請求結果……");
    try {
      const result = await requestJson(`/api/v1/conversations/${active.conversationId}/turns`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": active.idempotencyKey },
        body: active.body,
      });
      if (result.status === 202 || result.payload.state === "processing") {
        active.turnId = result.payload.turn_id;
        state.pollDeadline = Date.now() + POLL_MAX_MS;
        addUserMessage(active.question, active.turnId);
        await pollTurn();
      } else {
        displayTerminal(result.payload, active.question);
      }
    } catch (error) {
      if (error.uncertain) {
        state.active.uncertain = true;
      } else {
        finishActive();
      }
      setStatus(errorText(error), true);
      setBusy(false);
    }
  }

  async function sendQuestion() {
    if (state.active) return;
    const question = elements.question.value;
    const length = [...question].length;
    if (length === 0 || !question.trim()) {
      setStatus("請先輸入問題。", true);
      return;
    }
    if (length > MAX_QUESTION_LENGTH) {
      setStatus("問題不可超過 20,000 字元。", true);
      return;
    }
    setBusy(true);
    setStatus("準備對話……");
    try {
      if (!state.conversationId) state.conversationId = await createConversation();
      state.active = {
        conversationId: state.conversationId,
        idempotencyKey: crypto.randomUUID(),
        question,
        body: JSON.stringify({ question, language: "zh-TW" }),
        turnId: null,
        uncertain: false,
      };
      const result = await requestJson(`/api/v1/conversations/${state.conversationId}/turns`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": state.active.idempotencyKey },
        body: state.active.body,
      });
      if (result.status === 202 || result.payload.state === "processing") {
        state.active.turnId = result.payload.turn_id;
        state.pollDeadline = Date.now() + POLL_MAX_MS;
        addUserMessage(question, state.active.turnId);
        setStatus("問題處理中，請稍候……");
        await pollTurn();
      } else {
        displayTerminal(result.payload, question);
      }
      elements.question.value = "";
      updateCounter();
    } catch (error) {
      if (error.uncertain && state.active) {
        state.active.uncertain = true;
      } else if (error.status === 422 && state.active && !state.active.turnId) {
        state.active = null;
      } else if (state.active) {
        finishActive();
      }
      setStatus(errorText(error), true);
      setBusy(false);
    }
  }

  function newConversation() {
    if (state.active) return;
    clearPolling();
    state.conversationId = null;
    state.messages = [];
    state.seenTurns.clear();
    elements.question.value = "";
    updateCounter();
    renderMessages();
    setStatus("已準備新的對話。");
  }

  elements.question.addEventListener("input", updateCounter);
  elements.question.addEventListener("compositionstart", () => { state.composing = true; });
  elements.question.addEventListener("compositionend", () => { state.composing = false; });
  elements.question.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && !event.isComposing && !state.composing) {
      event.preventDefault();
      void sendQuestion();
    }
  });
  elements.send.addEventListener("click", () => { void sendQuestion(); });
  elements.confirm.addEventListener("click", () => { void submitExisting(); });
  elements.newConversation.addEventListener("click", newConversation);
  updateCounter();
  renderMessages();
})();
