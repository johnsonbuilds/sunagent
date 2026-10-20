/* SunAgent web UI client.
 *
 * The browser is the consumer of the frame stream produced by
 * agent_runtime.channels.webui. Frames arrive over SSE and are upserted
 * into the transcript by block id, so a live run and a replayed
 * transcript share one rendering path.
 */
(function () {
  "use strict";

  var MD = window.SunMarkdown;

  var SEND_ICON =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>';
  var STOP_ICON =
    '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>';

  var SUGGESTIONS = [
    "What files are in the current directory?",
    "Show me the project structure and explain what each part does.",
    "Run the test suite and summarise the failures.",
    "Read the README and give me a short orientation."
  ];

  var state = {
    sessions: [],
    activeId: null,
    blocks: new Map(),
    stream: null,
    lastSeq: 0,
    busy: false,
    pinned: true,
    config: null
  };

  var el = {
    sidebar: document.getElementById("sidebar"),
    sessionList: document.getElementById("sessionList"),
    sessionSearch: document.getElementById("sessionSearch"),
    sessionCount: document.getElementById("sessionCount"),
    sessionTitle: document.getElementById("sessionTitle"),
    statusBadge: document.getElementById("statusBadge"),
    modelChip: document.getElementById("modelChip"),
    messages: document.getElementById("messages"),
    messagesInner: document.getElementById("messagesInner"),
    emptyState: document.getElementById("emptyState"),
    suggestions: document.getElementById("suggestions"),
    input: document.getElementById("msgInput"),
    sendBtn: document.getElementById("btnSend"),
    toast: document.getElementById("toast"),
    themeBtn: document.getElementById("btnTheme"),
    sidebarBtn: document.getElementById("btnSidebar"),
    newChatBtn: document.getElementById("btnNewChat"),
    newChatSideBtn: document.getElementById("btnNewChatSide")
  };

  /* ------------------------------------------------------------- helpers */

  async function api(path, options) {
    var config = Object.assign(
      { headers: { "Content-Type": "application/json" } },
      options || {}
    );
    var response = await fetch(path, config);
    if (!response.ok) {
      var detail = response.statusText;
      try {
        var body = await response.json();
        if (body && body.detail) detail = body.detail;
      } catch (err) {
        /* the response was not JSON */
      }
      throw new Error(detail);
    }
    if (response.status === 204) return null;
    return response.json();
  }

  function formatValue(value) {
    if (typeof value === "string") return value;
    try {
      return JSON.stringify(value);
    } catch (err) {
      return String(value);
    }
  }

  function relativeTime(epochSeconds) {
    var delta = Date.now() / 1000 - epochSeconds;
    if (delta < 60) return "now";
    if (delta < 3600) return Math.floor(delta / 60) + "m";
    if (delta < 86400) return Math.floor(delta / 3600) + "h";
    return Math.floor(delta / 86400) + "d";
  }

  var toastTimer = null;

  function showToast(message) {
    el.toast.textContent = message;
    el.toast.hidden = false;
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      el.toast.hidden = true;
    }, 5000);
  }

  function setStatus(text, kind) {
    if (!text) {
      el.statusBadge.hidden = true;
      el.statusBadge.removeAttribute("data-state");
      return;
    }
    el.statusBadge.hidden = false;
    el.statusBadge.textContent = text;
    if (kind) el.statusBadge.setAttribute("data-state", kind);
    else el.statusBadge.removeAttribute("data-state");
  }

  function setBusy(busy) {
    state.busy = busy;
    el.sendBtn.classList.toggle("stop", busy);
    el.sendBtn.innerHTML = busy ? STOP_ICON : SEND_ICON;
    el.sendBtn.title = busy ? "Stop" : "Send";
    if (busy) setStatus("running");
    else setStatus(null);
  }

  /* -------------------------------------------------------------- blocks */

  function ensureBlock(id, kind) {
    var block = state.blocks.get(id);
    if (block) return block;

    var wrapper = document.createElement("div");
    var body;
    var head;

    if (kind === "user") {
      wrapper.className = "msg-row";
      wrapper.setAttribute("data-role", "user");
      body = document.createElement("div");
      body.className = "msg-bubble";
      wrapper.appendChild(body);
    } else if (kind === "assistant") {
      wrapper.className = "msg-row";
      wrapper.setAttribute("data-role", "assistant");
      var label = document.createElement("div");
      label.className = "msg-label";
      label.textContent = "SunAgent";
      body = document.createElement("div");
      body.className = "msg-body";
      wrapper.appendChild(label);
      wrapper.appendChild(body);
    } else if (kind === "reasoning") {
      wrapper.className = "thinking-card";
      head = document.createElement("button");
      head.type = "button";
      head.className = "thinking-head";
      head.innerHTML = "<span>Thinking</span><span class=\"thinking-caret\">›</span>";
      body = document.createElement("div");
      body.className = "thinking-body";
      var pre = document.createElement("pre");
      body.appendChild(pre);
      head.addEventListener("click", function () {
        wrapper.classList.toggle("open");
      });
      wrapper.appendChild(head);
      wrapper.appendChild(body);
      body = pre;
    } else {
      wrapper.className = "tool-card";
      wrapper.setAttribute("data-status", "running");
      head = document.createElement("button");
      head.type = "button";
      head.className = "tool-head";
      head.innerHTML =
        '<span class="tool-dot"></span>' +
        '<span class="tool-name"></span>' +
        '<span class="tool-summary"></span>' +
        '<span class="tool-meta"></span>' +
        '<span class="tool-caret">›</span>';
      body = document.createElement("div");
      body.className = "tool-body";
      head.addEventListener("click", function () {
        wrapper.classList.toggle("open");
      });
      wrapper.appendChild(head);
      wrapper.appendChild(body);
    }

    block = { el: wrapper, kind: kind, text: "", body: body, head: head };
    state.blocks.set(id, block);
    el.messagesInner.appendChild(wrapper);
    return block;
  }

  function updateEmptyState() {
    var hasBlocks = state.blocks.size > 0;
    if (hasBlocks && el.emptyState.parentNode) {
      el.emptyState.parentNode.removeChild(el.emptyState);
    } else if (!hasBlocks && !el.emptyState.parentNode) {
      el.messagesInner.appendChild(el.emptyState);
    }
  }

  function applyToolState(block, data) {
    var nameEl = block.head.querySelector(".tool-name");
    var summaryEl = block.head.querySelector(".tool-summary");
    var metaEl = block.head.querySelector(".tool-meta");
    nameEl.textContent = data.tool || "tool";
    summaryEl.textContent = data.summary || "";
    var meta = "";
    if (data.duration !== null && data.duration !== undefined) {
      meta += Number(data.duration).toFixed(2) + "s";
    }
    if (data.exit_code !== null && data.exit_code !== undefined && data.exit_code !== 0) {
      meta += (meta ? " · " : "") + "exit " + data.exit_code;
    }
    metaEl.textContent = meta;
    block.el.setAttribute("data-status", data.status || "running");

    block.body.innerHTML = "";
    var args = data.arguments;
    if (args && typeof args === "object" && Object.keys(args).length) {
      var argsBox = document.createElement("div");
      argsBox.className = "tool-args";
      Object.keys(args).forEach(function (key) {
        var row = document.createElement("div");
        row.className = "tool-arg";
        var keyEl = document.createElement("span");
        keyEl.className = "tool-arg-key";
        keyEl.textContent = key;
        var valueEl = document.createElement("span");
        valueEl.className = "tool-arg-val";
        valueEl.textContent = formatValue(args[key]);
        row.appendChild(keyEl);
        row.appendChild(valueEl);
        argsBox.appendChild(row);
      });
      block.body.appendChild(argsBox);
    }
    if (data.error) {
      var errorEl = document.createElement("div");
      errorEl.className = "tool-error";
      errorEl.textContent = data.error;
      block.body.appendChild(errorEl);
    }
    if (data.output) {
      var outputEl = document.createElement("pre");
      outputEl.className = "tool-output";
      outputEl.textContent = data.output;
      block.body.appendChild(outputEl);
    }
  }

  function upsertSegment(segment) {
    if (!segment || !segment.id) return;
    var block = ensureBlock(segment.id, segment.type);
    if (segment.type === "tool") {
      applyToolState(block, segment);
    } else {
      block.text = segment.content || "";
      if (segment.type === "assistant") {
        block.body.innerHTML = MD.render(block.text);
      } else {
        block.body.textContent = block.text;
      }
    }
    updateEmptyState();
    scheduleScroll();
  }

  function upsertDelta(frame) {
    var block = ensureBlock(frame.id, frame.channel);
    block.text += frame.text || "";
    if (frame.channel === "assistant") {
      block.body.innerHTML = MD.render(block.text);
    } else {
      block.body.textContent = block.text;
    }
    updateEmptyState();
    scheduleScroll();
  }

  function upsertTool(frame) {
    var block = ensureBlock(frame.id, "tool");
    applyToolState(block, frame);
    updateEmptyState();
    scheduleScroll();
  }

  var scrollScheduled = false;

  function scheduleScroll() {
    if (scrollScheduled) return;
    scrollScheduled = true;
    requestAnimationFrame(function () {
      scrollScheduled = false;
      if (!state.pinned) return;
      el.messages.scrollTop = el.messages.scrollHeight;
    });
  }

  /* ----------------------------------------------------------- transport */

  function handleFrame(frame) {
    if (typeof frame.seq === "number") {
      if (frame.seq <= state.lastSeq) return;
      state.lastSeq = frame.seq;
    }
    switch (frame.type) {
      case "run.started":
        setBusy(true);
        break;
      case "delta":
        upsertDelta(frame);
        break;
      case "tool.started":
      case "tool.completed":
      case "tool.failed":
        upsertTool(frame);
        break;
      case "segment":
        upsertSegment(frame.segment);
        break;
      case "run.completed":
        setBusy(false);
        refreshSessions();
        break;
      case "run.error":
        setBusy(false);
        setStatus("error", "error");
        showToast(frame.error || "The run failed");
        refreshSessions();
        break;
      default:
        break;
    }
  }

  function connectStream() {
    disconnectStream();
    if (!state.activeId) return;
    var source = new EventSource("/api/sessions/" + state.activeId + "/events");
    state.stream = source;
    source.onmessage = function (event) {
      var frame;
      try {
        frame = JSON.parse(event.data);
      } catch (err) {
        return;
      }
      handleFrame(frame);
    };
    source.onerror = function () {
      // The browser reconnects on its own and replays with
      // Last-Event-ID, so the stream recovers without user action.
      setStatus("reconnecting", "error");
    };
  }

  function disconnectStream() {
    if (state.stream) {
      state.stream.close();
      state.stream = null;
    }
  }

  /* ------------------------------------------------------------ sessions */

  function renderSessionList() {
    var filter = (el.sessionSearch.value || "").trim().toLowerCase();
    el.sessionList.innerHTML = "";
    var visible = state.sessions.filter(function (session) {
      return !filter || (session.title || "").toLowerCase().indexOf(filter) !== -1;
    });

    visible.forEach(function (session) {
      var item = document.createElement("div");
      item.className = "session-item" + (session.id === state.activeId ? " active" : "");
      item.title = session.title;

      var title = document.createElement("span");
      title.className = "session-title";
      title.textContent = session.title || "Untitled";
      title.addEventListener("dblclick", function (event) {
        event.stopPropagation();
        renameSession(session);
      });

      var time = document.createElement("span");
      time.className = "session-time";
      time.textContent = relativeTime(session.updated_at);

      var remove = document.createElement("button");
      remove.className = "session-delete";
      remove.type = "button";
      remove.title = "Delete conversation";
      remove.innerHTML =
        '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" ' +
        'stroke-width="1.8" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>';
      remove.addEventListener("click", function (event) {
        event.stopPropagation();
        deleteSession(session);
      });

      item.appendChild(title);
      item.appendChild(time);
      item.appendChild(remove);
      item.addEventListener("click", function () {
        selectSession(session.id);
      });
      el.sessionList.appendChild(item);
    });

    var count = state.sessions.length;
    el.sessionCount.textContent = count + (count === 1 ? " conversation" : " conversations");
  }

  async function refreshSessions() {
    state.sessions = await api("/api/sessions");
    renderSessionList();
  }

  async function selectSession(id) {
    disconnectStream();
    state.activeId = id;
    state.lastSeq = 0;
    state.pinned = true;
    state.blocks.clear();
    el.messagesInner.innerHTML = "";
    setBusy(false);

    var detail = await api("/api/sessions/" + id);
    el.sessionTitle.textContent = detail.title || "New conversation";
    (detail.segments || []).forEach(function (segment) {
      upsertSegment(segment);
    });
    updateEmptyState();
    renderSessionList();
    connectStream();
    el.messages.scrollTop = el.messages.scrollHeight;
    // On narrow screens the sidebar floats over the transcript, so it
    // gets out of the way once a conversation is picked.
    if (isNarrow()) el.sidebar.classList.add("collapsed");
  }

  async function newSession() {
    try {
      var session = await api("/api/sessions", { method: "POST", body: "{}" });
      await refreshSessions();
      await selectSession(session.id);
      el.input.focus();
    } catch (err) {
      showToast(err.message);
    }
  }

  async function renameSession(session) {
    var title = window.prompt("Conversation title", session.title || "");
    if (title === null) return;
    try {
      await api("/api/sessions/" + session.id, {
        method: "PATCH",
        body: JSON.stringify({ title: title })
      });
      await refreshSessions();
      if (session.id === state.activeId) {
        el.sessionTitle.textContent = title.trim() || session.title;
      }
    } catch (err) {
      showToast(err.message);
    }
  }

  async function deleteSession(session) {
    if (!window.confirm('Delete "' + (session.title || "this conversation") + '"?')) return;
    try {
      await api("/api/sessions/" + session.id, { method: "DELETE" });
      await refreshSessions();
      if (session.id === state.activeId) {
        if (state.sessions.length) await selectSession(state.sessions[0].id);
        else await newSession();
      }
    } catch (err) {
      showToast(err.message);
    }
  }

  /* -------------------------------------------------------------- sending */

  async function send() {
    var text = el.input.value.trim();
    if (!text || state.busy || !state.activeId) return;
    el.input.value = "";
    autoResize();
    upsertSegment({
      id: "local-" + Date.now() + "-" + Math.random().toString(16).slice(2),
      type: "user",
      content: text
    });
    try {
      await api("/api/sessions/" + state.activeId + "/messages", {
        method: "POST",
        body: JSON.stringify({ message: text })
      });
      setBusy(true);
    } catch (err) {
      setBusy(false);
      showToast(err.message);
    }
  }

  async function stop() {
    if (!state.activeId) return;
    try {
      await api("/api/sessions/" + state.activeId + "/cancel", {
        method: "POST",
        body: "{}"
      });
    } catch (err) {
      showToast(err.message);
    }
  }

  function autoResize() {
    el.input.style.height = "auto";
    el.input.style.height = Math.min(el.input.scrollHeight, 220) + "px";
  }

  function isNarrow() {
    return window.innerWidth < 860;
  }

  /* ---------------------------------------------------------------- theme */

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    try {
      window.localStorage.setItem("sunagent-theme", theme);
    } catch (err) {
      /* private mode */
    }
  }

  function toggleTheme() {
    var current = document.documentElement.getAttribute("data-theme");
    applyTheme(current === "dark" ? "light" : "dark");
  }

  /* ----------------------------------------------------------------- boot */

  function renderSuggestions() {
    el.suggestions.innerHTML = "";
    SUGGESTIONS.forEach(function (text) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "suggestion";
      button.textContent = text;
      button.addEventListener("click", function () {
        el.input.value = text;
        autoResize();
        send();
      });
      el.suggestions.appendChild(button);
    });
  }

  function bind() {
    el.sendBtn.addEventListener("click", function () {
      if (state.busy) stop();
      else send();
    });

    el.input.addEventListener("input", autoResize);
    el.input.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        send();
      }
    });

    el.sessionSearch.addEventListener("input", renderSessionList);
    el.themeBtn.addEventListener("click", toggleTheme);
    el.newChatBtn.addEventListener("click", newSession);
    el.newChatSideBtn.addEventListener("click", newSession);
    el.sidebarBtn.addEventListener("click", function () {
      el.sidebar.classList.toggle("collapsed");
    });

    el.messages.addEventListener("scroll", function () {
      var distance = el.messages.scrollHeight - el.messages.scrollTop - el.messages.clientHeight;
      state.pinned = distance < 120;
    });

    document.addEventListener("keydown", function (event) {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        newSession();
      }
    });
  }

  async function boot() {
    var stored = null;
    try {
      stored = window.localStorage.getItem("sunagent-theme");
    } catch (err) {
      /* private mode */
    }
    applyTheme(stored || "dark");
    bind();
    renderSuggestions();
    if (isNarrow()) el.sidebar.classList.add("collapsed");

    try {
      state.config = await api("/api/config");
      var parts = [];
      if (state.config.model) parts.push(state.config.model);
      if (state.config.harness) parts.push(state.config.harness);
      el.modelChip.textContent = parts.join(" · ");
      el.modelChip.title = "model " + (state.config.model || "?") +
        " · harness " + (state.config.harness || "?");
      if (state.config.error) showToast(state.config.error);
    } catch (err) {
      el.modelChip.textContent = "";
    }

    await refreshSessions();
    if (!state.sessions.length) {
      await newSession();
    } else {
      await selectSession(state.sessions[0].id);
    }
  }

  boot();
})();
