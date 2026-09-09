(() => {
  const port = new URLSearchParams(location.search).get("port") || "8765";
  // Which tab this WebView2 instance belongs to (see MainWindow's tab strip,
  // each tab navigates to chat.html?tab=<id>) -- threaded into the WS URL so
  // the backend routes this connection to the right per-tab ChatSession
  // (server.ts's `sessions` map), and into the localStorage transcript key
  // below so tabs sharing one WebView2 profile/origin don't overwrite each
  // other's chat history.
  const tabId = new URLSearchParams(location.search).get("tab") || "1";
  // Native/WPF-only setting (MainWindow.Topmost) -- the backend has no stake in it at
  // all, so it's read once, synchronously, from the query string MainWindow.xaml.cs
  // already stamps onto this page's own URL, same as port/tab above, rather than
  // waiting on a postMessage round trip after load.
  const initialAlwaysOnTop = new URLSearchParams(location.search).get("alwaysOnTop") !== "0";
  const messagesEl = document.getElementById("messages");
  const statusBarText = document.getElementById("statusBarText");
  const lampBackend = document.getElementById("lampBackend");
  const lampChannel = document.getElementById("lampChannel");
  const inputEl = document.getElementById("input");
  const sendBtn = document.getElementById("sendBtn");
  const stopBtn = document.getElementById("stopBtn");

  const settingsBtn = document.getElementById("settingsBtn");
  const closeSettingsBtn = document.getElementById("closeSettingsBtn");
  const settingsOverlay = document.getElementById("settingsOverlay");
  const authStatusText = document.getElementById("authStatusText");
  const authLoginOutput = document.getElementById("authLoginOutput");
  const loginBtn = document.getElementById("loginBtn");
  const logoutBtn = document.getElementById("logoutBtn");
  const mcpListOutput = document.getElementById("mcpListOutput");
  const mcpRefreshBtn = document.getElementById("mcpRefreshBtn");
  const mcpAddBtn = document.getElementById("mcpAddBtn");
  const mcpRemoveBtn = document.getElementById("mcpRemoveBtn");

  const modeStatusText = document.getElementById("modeStatusText");
  const ownAnthropicKeyInput = document.getElementById("ownAnthropicKeyInput");
  const ownAnthropicKeySaveBtn = document.getElementById("ownAnthropicKeySaveBtn");
  const ownAnthropicKeyClearBtn = document.getElementById("ownAnthropicKeyClearBtn");
  const ownAnthropicKeyStatus = document.getElementById("ownAnthropicKeyStatus");
  const swAccountStatus = document.getElementById("swAccountStatus");
  const swUpsellHint = document.getElementById("swUpsellHint");
  const swLoginBtn = document.getElementById("swLoginBtn");

  const smsAccountStatus = document.getElementById("smsAccountStatus");
  const smsApiKeyInput = document.getElementById("smsApiKeyInput");
  const smsSenderInput = document.getElementById("smsSenderInput");
  const smsAccountSaveBtn = document.getElementById("smsAccountSaveBtn");
  const smsAccountRemoveBtn = document.getElementById("smsAccountRemoveBtn");

  // mode_get and sw_status are two independent control-op round trips (see their
  // handlers below) -- cached here so whichever one resolves last can still decide
  // whether to show the "connect SquirrelWisdom too" hint, which depends on BOTH.
  let lastChatSource = null;
  let lastSwLoggedIn = null;

  // Per explicit instruction (2026-09-07): a user paying via their own Claude
  // account has no reason to know SquirrelWisdom exists, let alone that logging
  // into it would also unlock Notes/Ratatosk/etc -- nothing ever suggested it
  // before. Only shown once both statuses are actually known (not mid-"Checking…").
  function renderSwUpsellHint() {
    const show = typeof lastChatSource === "string" && lastChatSource.startsWith("own-anthropic-") && lastSwLoggedIn === false;
    swUpsellHint.style.display = show ? "" : "none";
  }
  const swTopUpBtn = document.getElementById("swTopUpBtn");

  const ratatoskStatusText = document.getElementById("ratatoskStatusText");
  const ratatoskRegisterBtn = document.getElementById("ratatoskRegisterBtn");

  const attachBtn = document.getElementById("attachBtn");
  const fileInput = document.getElementById("fileInput");
  const attachmentsEl = document.getElementById("attachments");
  const micBtn = document.getElementById("micBtn");

  const personaProfile = document.getElementById("personaProfile");
  const personaCustomFields = document.getElementById("personaCustomFields");
  const personaStandardFields = document.getElementById("personaStandardFields");
  const personaName = document.getElementById("personaName");
  const personaGender = document.getElementById("personaGender");
  const personaAge = document.getElementById("personaAge");
  const personaBio = document.getElementById("personaBio");
  const personaBiography = document.getElementById("personaBiography");
  const personaPhotosDir = document.getElementById("personaPhotosDir");
  const personaSaveBtn = document.getElementById("personaSaveBtn");
  const personaResetBtn = document.getElementById("personaResetBtn");

  // Name/gender/age/bio are always shown -- for a standard profile they're
  // an override on top of its built-in identity, same fields either way.
  // Biography/photos-folder and the reset button only make sense for a
  // standard profile (custom has no built-in to override or reset to).
  function updatePersonaFieldsVisibility() {
    const isStandard = personaProfile.value !== "custom";
    personaStandardFields.style.display = isStandard ? "" : "none";
    personaResetBtn.style.display = isStandard ? "" : "none";
  }
  personaProfile.addEventListener("change", updatePersonaFieldsVisibility);
  const alwaysOnTop = document.getElementById("alwaysOnTop");
  alwaysOnTop.checked = initialAlwaysOnTop;
  alwaysOnTop.addEventListener("change", () => {
    if (window.chrome?.webview) window.chrome.webview.postMessage({ type: "set_always_on_top", value: alwaysOnTop.checked });
  });
  const voiceAutoSend = document.getElementById("voiceAutoSend");
  const visualModeEnabled = document.getElementById("visualModeEnabled");
  const visualModeUnavailableHint = document.getElementById("visualModeUnavailableHint");
  visualModeEnabled.addEventListener("change", () => {
    visualModeState.enabled = visualModeEnabled.checked;
    sendControl("visual_mode_set", { enabled: visualModeEnabled.checked });
  });

  // Populated from "visual_mode_get" (settings check + startup fetch below) and
  // kept live by the "visual_mode_config" push the backend sends once at Caroline's
  // own startup (see server.ts's OutEvent doc comment) -- playOneSpeech reads this
  // to decide whether a given voice reply should go to the native Visual Mode
  // window instead of the in-page <audio> player.
  let visualModeState = { enabled: false, available: false };

  let ws = null;
  let turnsInFlight = 0;
  let pendingAttachments = []; // [{name, mimeType, dataBase64}]
  // One entry per turn that's been sent but hasn't had its "result" yet, in
  // the order they were sent -- the backend/SDK process turns strictly in
  // that same order (one input stream, one query() loop), so the oldest
  // entry here always corresponds to whichever turn is currently producing
  // output. Needed because sending is no longer blocked while busy: without
  // this, a second message sent before the first's result arrives would
  // stomp on the first turn's in-progress voice-playback bookkeeping.
  let turnQueue = [];

  // Everything that used to be three separate displays (header connection
  // text, in-chat system banners, the tool-status heartbeat strip) now goes
  // through this one bottom status bar -- per explicit design ask, a single
  // consolidated place instead of scattered status text. Most messages
  // (connection state, the tool heartbeat) are persistent until explicitly
  // replaced; a one-off banner (a transcription error, a file that was too
  // large, etc.) is transient -- it shows briefly, then the bar reverts to
  // whatever connection status text was showing before it, same idea as a
  // toast notification.
  let lastConnText = "connecting…";
  let lastConnCls = null;
  let statusBarRevertTimer = null;

  function setStatusBarText(text, transientMs) {
    statusBarText.textContent = text;
    if (statusBarRevertTimer) {
      clearTimeout(statusBarRevertTimer);
      statusBarRevertTimer = null;
    }
    if (transientMs) {
      statusBarRevertTimer = setTimeout(() => {
        statusBarText.textContent = lastConnText;
        statusBarRevertTimer = null;
      }, transientMs);
    }
  }

  // Lamp 1 (backend/connection): red = error, yellow = connecting/
  // reconnecting, green = connected, green-blinking = a turn is actively
  // running. Driven by the same (text, cls) pairs setStatus already receives
  // from every call site (WS open/close, caroline_status messages) --
  // cls values are "connected" | "restarting" | "error" | undefined (the
  // initial "connecting…" state before the first WS open).
  function updateBackendLamp() {
    let color;
    if (lastConnCls === "connected") color = turnBusy ? "green-blink" : "green";
    else if (lastConnCls === "error") color = "red";
    else color = "yellow"; // "restarting" (reconnecting/recovering) or initial
    lampBackend.className = "lamp lamp-" + color;
  }

  // Tracks the actual WebSocket connection only -- NOT the same thing as
  // lastConnCls, which also carries "error" for in-app notices (a depleted
  // API/subscription balance, a repeated-failure banner) that have nothing to
  // do with whether this page can reach the backend. Confirmed live
  // (2026-09-03): hitting the monthly Claude subscription limit sent a
  // system_notice with cls "error", which setStatus used to treat exactly
  // like a dead socket and disabled the input field over it -- even though
  // the WS was fine and the user could see and read the notice just fine.
  let wsConnected = false;

  function setStatus(text, cls) {
    // Per explicit instruction (2026-09-06): confirmed live that the lamp can
    // end up showing "restarting" (yellow) while the backend's own connState
    // for this tab is already "connected" -- a genuine desync whose cause
    // wasn't pinned down. Every call here (from whichever source -- ws
    // caroline_status/system_notice, or the native update_status message)
    // is now visible in this WebView2's own devtools console, so a future
    // occurrence can be traced client-side too, not just from the backend.
    console.log(`[caroline] setStatus text=${JSON.stringify(text)} cls=${cls || "(none)"} (was lastConnCls=${lastConnCls})`);
    lastConnText = text;
    lastConnCls = cls || null;
    setStatusBarText(text);
    updateBackendLamp();
    // Per explicit correction (2026-09-03): only the input field (and voice
    // input -- sending a transcribed message has the exact same "nowhere to
    // send it yet" problem while not connected, confirmed live as a gap the
    // text field's own disabling missed) gets disabled while not connected --
    // the rest of the UI (tab strip, menu, everything else) must stay fully
    // usable the whole time. A previous version blocked the ENTIRE main
    // window instead; that was wrong and has been removed (see App.xaml.cs).
    //
    // "not connected" here means the WS itself, not lastConnCls -- an SDK/
    // billing/limit problem (also surfaced via cls "error") is a reason to
    // show a red lamp, not a reason to stop the user from typing (see this
    // function's own wsConnected comment above).
    inputEl.disabled = !wsConnected;
    micBtn.disabled = !wsConnected;
  }

  // Lamp 2 (Ratatosk channel -- every group Caroline's own account is a
  // member of, not just one DM, see ratatoskChannel.ts): yellow = not
  // connected/configured/monitored, red = the channel's most recent poll
  // tick threw, green = monitoring with nothing new, green-blinking = the
  // most recent tick actually found and injected new message(s). Polled (no
  // push exists for this today) at the same cadence as the channel's own
  // poll interval -- see ratatoskChannel.ts's POLL_INTERVAL_MS.
  function updateChannelLamp(status) {
    let color;
    if (!status || !status.enabled) color = "yellow";
    else if (status.lastTickOutcome && status.lastTickOutcome.startsWith("threw:")) color = "red";
    else if (status.lastTickOutcome && status.lastTickOutcome.includes("injecting into headless session")) color = "green-blink";
    else color = "green";
    lampChannel.className = "lamp lamp-" + color;
  }

  function pollChannelStatus() {
    sendControl("ratatosk_channel_status");
  }

  function scrollToEnd() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  // Persists the *displayed* transcript across app restarts, in this
  // WebView2 profile's localStorage (the profile itself is already
  // persistent -- see MainWindow's CoreWebView2Environment userDataFolder).
  // The model's own memory of the conversation persists separately via the
  // backend's own per-tab resume session id (see server.ts) -- this is just
  // so the UI isn't blank on reopen. Capped so it can't grow unbounded.
  // Keyed by tabId -- all tabs share one WebView2 profile/origin, so without
  // this every tab would read/overwrite the exact same localStorage entry.
  const TRANSCRIPT_KEY = `caroline:transcript:${tabId}`;
  const TRANSCRIPT_MAX = 300;

  function loadTranscript() {
    try {
      const raw = localStorage.getItem(TRANSCRIPT_KEY);
      return raw ? JSON.parse(raw) : [];
    } catch {
      return [];
    }
  }

  function saveTranscriptEntry(role, text, attachments, ts) {
    try {
      const list = loadTranscript();
      list.push({ role, text, attachments: (attachments || []).map((a) => ({ name: a.name, mimeType: a.mimeType })), ts });
      while (list.length > TRANSCRIPT_MAX) list.shift();
      localStorage.setItem(TRANSCRIPT_KEY, JSON.stringify(list));
    } catch { /* private-browsing/quota -- transcript just won't survive a restart */ }
  }

  // Set once a get_history request has been sent (see connect()'s "open"
  // handler below) -- sendControl needs an open WS, which isn't ready yet
  // at replayTranscript()'s own call time (before connect()), and this
  // also stops a reconnect from re-requesting it every time.
  let historyRequested = false;

  function replayTranscript() {
    const entries = loadTranscript();
    for (const entry of entries) {
      addBubble(entry.role, entry.text, entry.attachments, { persist: false, ts: entry.ts });
    }
    if (entries.length > 0) {
      addStatusLine("— earlier conversation restored —");
    }
  }

  function formatTimestamp(ts) {
    if (!ts) return "";
    const d = new Date(ts);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  // Tracks the calendar day of the last bubble actually rendered, across
  // both replayed history and live messages -- a fresh day (including one
  // that arrives mid-session, past midnight) gets a "Today"/"Yesterday"/
  // full-date separator line, same idea as most chat apps.
  let lastBubbleDateKey = null;

  function dateSeparatorLabelIfNeeded(ts) {
    if (!ts) return null;
    const d = new Date(ts);
    const key = d.toDateString();
    if (key === lastBubbleDateKey) return null;
    lastBubbleDateKey = key;

    const today = new Date();
    const yesterday = new Date(today);
    yesterday.setDate(today.getDate() - 1);
    if (key === today.toDateString()) return "Today";
    if (key === yesterday.toDateString()) return "Yesterday";
    return d.toLocaleDateString([], { weekday: "long", year: "numeric", month: "long", day: "numeric" });
  }

  function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  // Minimal markdown -> HTML: bold, italic, inline code, links, headings,
  // and "- "/"* " bullet lines. Not a full parser -- covers what Claude's
  // replies actually use. Escapes HTML first so nothing in the model's
  // output (or a page it quotes) can inject markup.
  function renderMarkdown(text) {
    let html = escapeHtml(text);

    // Images/links/code are pulled out into placeholder tokens FIRST and
    // substituted back in at the very end -- NOT just reordered ahead of
    // bold/italic, which was tried and confirmed still broken: every
    // .replace() call below scans the ENTIRE current string, so even
    // running the image rule "first" doesn't protect its own output from
    // the _italic_ rule a few lines later -- it still finds the
    // underscores inside the already-inserted <img src="assets/caroline_
    // looks/...png"> and mangles them into <em> tags, corrupting the URL
    // (confirmed live via DevTools: the resolved src literally contained
    // "caroline%3Cem%3Elooks"). Placeholders contain no markdown-special
    // characters at all, so nothing later can touch them.
    const placeholders = [];
    function stash(html_) {
      const token = ` P${placeholders.length} `;
      placeholders.push(html_);
      return token;
    }

    html = html.replace(/```([\s\S]*?)```/g, (_, code) => stash(`<pre><code>${code}</code></pre>`));
    html = html.replace(/`([^`]+)`/g, (_, code) => stash(`<code>${code}</code>`));

    // Only local paths under assets/ -- deliberately not remote URLs, so the
    // model can show her own shipped photos but can't embed arbitrary
    // external images into the chat.
    html = html.replace(/!\[([^\]]*)\]\((assets\/[^\s)"']+)\)/g,
      (_, alt, src) => stash(`<img class="chat-photo" alt="${alt}" title="${alt}" src="${src}" />`));

    // A local document link -- clicking it asks the backend to open it in
    // the OS default app (see the "open_file" control op) rather than
    // trying to navigate the page itself to a file:// URL.
    html = html.replace(/\[([^\]]+)\]\(file:\/\/\/([^\s)"']+)\)/g,
      (_, label, path) => stash(`<a href="#" class="doc-link" data-path="${decodeURIComponent(path)}">📄 ${label}</a>`));

    html = html.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      (_, label, url) => stash(`<a href="${url}" target="_blank" rel="noopener">${label}</a>`));
    // Bare URLs not already wrapped in an <a> tag from the rule above.
    html = html.replace(/(^|[^"'>])(https?:\/\/[^\s<]+)/g,
      (_, pre, url) => pre + stash(`<a href="${url}" target="_blank" rel="noopener">${url}</a>`));

    html = html.replace(/^###\s+(.+)$/gm, "<h4>$1</h4>");
    html = html.replace(/^##\s+(.+)$/gm, "<h3>$1</h3>");
    html = html.replace(/^#\s+(.+)$/gm, "<h2>$1</h2>");

    html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/__([^_]+)__/g, "<strong>$1</strong>");
    html = html.replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, "<em>$1</em>");
    html = html.replace(/(?<!_)_([^_]+)_(?!_)/g, "<em>$1</em>");

    // GFM-style pipe tables (| a | b |\n| --- | --- |\n| c | d |). Run AFTER
    // bold/italic so cell text already has its inline formatting resolved
    // (placeholders from the code/link/image rules above are plain tokens
    // with no "|" in them, so splitting rows on "|" is safe either way).
    // Confirmed live (2026-09-01): Caroline kept sending raw "| a | b |"
    // text that rendered as literal pipe characters instead of a table --
    // this was previously never actually parsed at all.
    html = convertMarkdownTables(html);

    // "- item" / "* item" lines -> <li>, consecutive runs wrapped in <ul>.
    html = html.replace(/^[-*]\s+(.+)$/gm, "<li>$1</li>");
    html = html.replace(/(<li>[\s\S]*?<\/li>\n?)+/g, (m) => `<ul>${m}</ul>`);

    // Put the protected HTML back, last -- after this, nothing else touches it.
    html = html.replace(/ P(\d+) /g, (_, i) => placeholders[Number(i)]);

    return html;
  }

  function splitTableRow(line) {
    let t = line.trim();
    if (t.startsWith("|")) t = t.slice(1);
    if (t.endsWith("|")) t = t.slice(0, -1);
    return t.split("|").map((c) => c.trim());
  }

  // A separator row is only "-", ":", "|" and whitespace, with at least one
  // dash -- distinguishes it from an ordinary row that merely starts with "|".
  function isTableSeparatorRow(line) {
    return /^[\s|:-]+$/.test(line) && line.includes("-");
  }

  function convertMarkdownTables(html) {
    const lines = html.split("\n");
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const header = lines[i];
      const sep = lines[i + 1];
      if (header && header.trim().startsWith("|") && sep && isTableSeparatorRow(sep)) {
        const headerCells = splitTableRow(header);
        let j = i + 2;
        const bodyRows = [];
        while (j < lines.length && lines[j].trim().startsWith("|")) {
          bodyRows.push(splitTableRow(lines[j]));
          j++;
        }
        const thead = `<tr>${headerCells.map((c) => `<th>${c}</th>`).join("")}</tr>`;
        const tbody = bodyRows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("");
        out.push(`<table class="md-table"><thead>${thead}</thead><tbody>${tbody}</tbody></table>`);
        i = j;
        continue;
      }
      out.push(header);
      i++;
    }
    return out.join("\n");
  }

  // Full inline preview when the base64 payload is still in hand (a
  // just-sent attachment); falls back to a plain named chip when it isn't
  // (attachment history reloaded from the transcript, which deliberately
  // doesn't persist the payload -- see saveTranscriptEntry).
  function renderAttachment(a) {
    const mime = a.mimeType || "";
    if (a.dataBase64 && mime.startsWith("image/")) {
      const img = document.createElement("img");
      img.className = "chat-photo attachment-preview";
      img.alt = a.name;
      img.title = a.name;
      img.src = `data:${mime};base64,${a.dataBase64}`;
      img.addEventListener("click", () => window.open(img.src, "_blank"));
      return img;
    }
    if (a.dataBase64 && mime.startsWith("video/")) {
      const video = document.createElement("video");
      video.className = "chat-photo attachment-preview";
      video.controls = true;
      video.src = `data:${mime};base64,${a.dataBase64}`;
      return video;
    }
    const chip = document.createElement(a.dataBase64 ? "a" : "div");
    chip.className = "attachment-chip";
    if (a.dataBase64) {
      chip.href = `data:${mime};base64,${a.dataBase64}`;
      chip.download = a.name;
      chip.title = `Open ${a.name}`;
    }
    chip.innerHTML = `<span>${mime.startsWith("image/") ? "🖼️" : "📄"}</span><span class="name"></span>`;
    chip.querySelector(".name").textContent = a.name;
    return chip;
  }

  // Per explicit instruction (2026-09-09): a recovered chat bubble
  // (get_history) that quotes one of the backend's dehydration/archive notes
  // is just a raw file path in prose -- useless to a human who isn't going
  // to go find and open that file by hand. Same regex as dehydrate.ts's
  // exported extractDehydratedFilePath -- must stay in sync with that
  // file's note templates, since chat.js can't import a backend module.
  function extractDehydratedFilePath(text) {
    const m = typeof text === "string" ? text.match(/[Сс]охранен[оа] в файле: (.+?)\. /) : null;
    return m ? m[1] : null;
  }

  let expandRequestSeq = 0;
  const pendingExpandRequests = new Map(); // requestId -> callback(ok, parsedStdout, stderr)

  /** Appends a "show more" link to `containerDiv` that fetches and inserts the referenced dehydrated content right after it, in place. */
  function addExpandLink(containerDiv, filePath) {
    const link = document.createElement("button");
    link.className = "expand-dehydrated-link";
    link.textContent = "Show archived content ▾";
    link.addEventListener("click", () => {
      link.disabled = true;
      link.textContent = "Loading…";
      const requestId = `expand-${++expandRequestSeq}`;
      pendingExpandRequests.set(requestId, (ok, parsed, err) => {
        pendingExpandRequests.delete(requestId);
        if (!ok || !parsed) {
          link.disabled = false;
          link.textContent = "Show archived content ▾ (failed, retry)";
          console.error("expand_dehydrated_ref failed:", err);
          return;
        }
        const insertAfter = (el) => containerDiv.parentNode.insertBefore(el, containerDiv.nextSibling);
        if (parsed.kind === "text") {
          // Insert in original order, each right after the previous one, so
          // the whole archived stretch reads top-to-bottom after the link.
          let anchor = containerDiv;
          for (const entry of parsed.entries) {
            const sub = document.createElement("div");
            sub.className = "bubble " + entry.role + " archived";
            const textDiv = document.createElement("div");
            if (entry.role === "assistant") textDiv.innerHTML = renderMarkdown(entry.text);
            else textDiv.textContent = entry.text;
            sub.appendChild(textDiv);
            anchor.parentNode.insertBefore(sub, anchor.nextSibling);
            anchor = sub;
          }
        } else if (parsed.kind === "media" && parsed.mimeType && parsed.mimeType.startsWith("image/")) {
          const img = document.createElement("img");
          img.className = "archived-image";
          img.src = `data:${parsed.mimeType};base64,${parsed.dataBase64}`;
          insertAfter(img);
        } else {
          const note = document.createElement("div");
          note.className = "bubble system archived";
          note.textContent = `[${parsed.mimeType || "file"} -- open manually: ${filePath}]`;
          insertAfter(note);
        }
        link.remove();
        scrollToEnd();
      });
      sendControl("expand_dehydrated_ref", { filePath, requestId });
    });
    containerDiv.appendChild(link);
  }

  function addBubble(role, text, attachments, opts) {
    const ts = (opts && opts.ts) || Date.now();
    if (!opts || opts.persist !== false) saveTranscriptEntry(role, text, attachments, ts);
    const dateLabel = dateSeparatorLabelIfNeeded(ts);
    if (dateLabel) addStatusLine(dateLabel);
    const div = document.createElement("div");
    div.className = "bubble " + role;
    for (const a of attachments || []) {
      div.appendChild(renderAttachment(a));
    }
    const textDiv = document.createElement("div");
    if (role === "assistant") textDiv.innerHTML = renderMarkdown(text);
    else textDiv.textContent = text;
    if (text) div.appendChild(textDiv);

    // Every assistant bubble gets a manual speak button, including
    // voice-originated turns (those ALSO auto-speak once via handleEvent's
    // "result" branch) -- confirmed live this was needed even then: auto-
    // speak only fires once, and without a button there was no way to
    // replay it afterward.
    if (role === "assistant" && text) {
      const speakBtn = document.createElement("button");
      speakBtn.className = "speak-btn";
      speakBtn.textContent = "🔊";
      speakBtn.title = "Read aloud";
      speakBtn.addEventListener("click", () => toggleSpeak(text, speakBtn));
      div.appendChild(speakBtn);
    }

    const timeDiv = document.createElement("div");
    timeDiv.className = "bubble-time";
    timeDiv.textContent = formatTimestamp(ts);
    div.appendChild(timeDiv);

    messagesEl.appendChild(div);
    scrollToEnd();
    return div;
  }

  // requestId -> callback. Both "tts" and "stt" control requests run
  // un-awaited on the backend (see server.ts's ws "message" handler), so
  // several in flight at once can genuinely finish out of request order --
  // confirmed live this broke a plain FIFO shift()-based pairing (a
  // shorter reply's audio finishing synthesis before an earlier, longer
  // one would get matched to the wrong pending entry). Matched by id
  // instead, regardless of arrival order.
  let pendingTtsRequests = new Map();
  let pendingSttRequests = new Map();
  let ttsRequestSeq = 0;
  let sttRequestSeq = 0;

  // Only one "read aloud" plays at a time; anything else requested while
  // one is active/queued waits its turn in speechQueue and plays in the
  // order it was REQUESTED, not the order its synthesis happens to finish
  // -- confirmed live multiple voice-turn replies could otherwise overlap
  // or play back out of order (each queued speak() call used to start
  // playing as soon as its own network round trip came back).
  let activeSpeech = null; // { btn, audio, aborted, resolveQueue }
  let speechQueue = [];
  let speechQueueRunning = false;

  function stopActiveSpeech() {
    if (!activeSpeech) return;
    activeSpeech.aborted = true;
    activeSpeech.btn?.classList.remove("speaking");
    try { activeSpeech.audio?.pause(); } catch {}
    if (activeSpeech.visual && window.chrome?.webview) {
      window.chrome.webview.postMessage({ type: "visual_speech_stop", requestId: activeSpeech.requestId });
      pendingVisualDone.delete(activeSpeech.requestId);
    }
    const resolveQueue = activeSpeech.resolveQueue;
    activeSpeech = null;
    hideVoiceControls();
    resolveQueue?.(); // let the queue move on instead of waiting forever for "ended"/"visual_speech_done"
  }

  // Small playback bar (pause/restart/stop) shown while a reply is being read
  // aloud -- separate from stopActiveSpeech's own manual-🔊-click handling
  // (toggleSpeak): that one only ever stops outright, this adds pause/resume
  // and restart-from-the-beginning too, requested explicitly since the
  // existing controls could only fully stop, never just pause.
  const voiceControlsEl = document.getElementById("voiceControls");
  const voicePauseBtn = document.getElementById("voicePauseBtn");
  const voiceRestartBtn = document.getElementById("voiceRestartBtn");
  const voiceStopBtn = document.getElementById("voiceStopBtn");

  function showVoiceControls() {
    if (voiceControlsEl) voiceControlsEl.hidden = false;
    updateVoicePauseBtnLabel();
  }

  function hideVoiceControls() {
    if (voiceControlsEl) voiceControlsEl.hidden = true;
  }

  function updateVoicePauseBtnLabel() {
    if (!voicePauseBtn) return;
    const paused = !!activeSpeech?.audio?.paused;
    voicePauseBtn.textContent = paused ? "▶" : "⏸";
    voicePauseBtn.title = paused ? "Resume" : "Pause";
  }

  voicePauseBtn?.addEventListener("click", () => {
    if (!activeSpeech?.audio) return;
    if (activeSpeech.audio.paused) activeSpeech.audio.play().catch(() => {});
    else { try { activeSpeech.audio.pause(); } catch {} }
    updateVoicePauseBtnLabel();
  });

  voiceRestartBtn?.addEventListener("click", () => {
    if (!activeSpeech?.audio) return;
    try {
      activeSpeech.audio.currentTime = 0;
      activeSpeech.audio.play().catch(() => {});
      updateVoicePauseBtnLabel();
    } catch {}
  });

  voiceStopBtn?.addEventListener("click", () => {
    stopActiveSpeech();
    speechQueue = [];
  });

  // Click handling for a message's manual 🔊 button: clicking the button
  // that's currently blinking (mid-synthesis or mid-playback) stops it
  // immediately; clicking a different one drops whatever's queued and
  // switches to that one instead -- a deliberate manual click takes
  // priority over auto-speak's own queue, not a place in line behind it.
  function toggleSpeak(text, btn) {
    if (activeSpeech && activeSpeech.btn === btn) {
      stopActiveSpeech();
      speechQueue = [];
      return;
    }
    stopActiveSpeech();
    speechQueue = [];
    speak(text, btn);
  }

  function speak(text, btn) {
    speechQueue.push({ text, btn });
    runSpeechQueue();
  }

  // Resolves once no voice-input recording session is active. Voice output
  // waits on this before starting each queued item -- confirmed this was
  // wanted so Caroline's own playback never talks over (or gets picked up
  // during) the user actively dictating. Polling rather than an event is
  // fine here: voiceSession only ever changes on user action (start/stop
  // click) or a segment finishing, none of which need sub-200ms reaction.
  function waitForVoiceInputIdle() {
    return new Promise((resolve) => {
      if (!voiceSession) { resolve(); return; }
      const check = () => { if (!voiceSession) resolve(); else setTimeout(check, 200); };
      check();
    });
  }

  async function runSpeechQueue() {
    if (speechQueueRunning) return;
    speechQueueRunning = true;
    while (speechQueue.length > 0) {
      await waitForVoiceInputIdle();
      const item = speechQueue.shift();
      await playOneSpeech(item);
    }
    speechQueueRunning = false;
  }

  // Per explicit correction (2026-09-03): Visual Mode now applies to BOTH the
  // automatic voice-reply narration (speak() called with no btn) AND a
  // manual per-message 🔊 click (btn set) -- same animation either way when
  // Visual Mode is on; `btn` no longer factors into the decision at all.
  // With Visual Mode off, playback is unaffected either way (plain
  // playAudioBase64, same as always).
  function shouldUseVisualMode(btn) {
    return visualModeState.enabled && visualModeState.available && !!window.chrome?.webview;
  }

  function playOneSpeech({ text, btn }) {
    return new Promise((resolve) => {
      const useVisual = shouldUseVisualMode(btn);
      const requestId = `tts-${++ttsRequestSeq}`;
      const entry = { btn, audio: null, aborted: false, resolveQueue: resolve, visual: useVisual, requestId };
      activeSpeech = entry;
      btn?.classList.add("speaking");
      if (useVisual) {
        // Fires the moment generation starts, per explicit spec -- the native
        // window appears with its static "silence" frame right away, well
        // before the audio (let alone the render) is actually ready.
        window.chrome.webview.postMessage({ type: "visual_speech_start", requestId });
      }
      pendingTtsRequests.set(requestId, (ok, audioBase64, err) => {
        if (entry.aborted) return; // stopActiveSpeech() already resolved this slot
        if (!ok || !audioBase64) {
          if (!ok) addBanner(`Could not read that aloud: ${err || "unknown error"}`);
          if (useVisual) window.chrome.webview.postMessage({ type: "visual_speech_cancel", requestId });
          if (activeSpeech === entry) activeSpeech = null;
          btn?.classList.remove("speaking");
          resolve();
          return;
        }
        if (useVisual) playAudioViaVisualMode(audioBase64, entry, resolve);
        else playAudioBase64(audioBase64, entry, resolve);
      });
      sendControl("tts", { text, requestId });
    });
  }

  // Native (VisualModeManager.cs) renders the talking-head video and plays it
  // (with its own muxed audio, its own pause/restart/stop controls -- see
  // VisualModeWindow) entirely on its own; this just hands off the audio and
  // waits for "visual_speech_done" (see the message listener below) the same
  // way playAudioBase64 waits for the <audio> element's "ended" event.
  let pendingVisualDone = new Map(); // requestId -> { base64, entry, onDone }
  function playAudioViaVisualMode(base64, entry, onDone) {
    if (entry.aborted) { onDone?.(); return; }
    pendingVisualDone.set(entry.requestId, { base64, entry, onDone });
    window.chrome.webview.postMessage({ type: "visual_speech_audio", requestId: entry.requestId, audioBase64: base64 });
  }

  const STT_TIMEOUT_MS = 30_000;

  function transcribe(audioBase64, format) {
    return new Promise((resolve) => {
      const requestId = `stt-${++sttRequestSeq}`;
      // If the connection drops (or reconnects) between sending this and
      // getting a "stt" control_response back, this entry would otherwise
      // sit in pendingSttRequests forever, unresolved -- confirmed live as
      // a real way to leave the mic button stuck on "transcribing"
      // indefinitely. A timed-out entry resolves to null (same shape as a
      // failed transcription) instead of hanging.
      const timer = setTimeout(() => {
        pendingSttRequests.delete(requestId);
        resolve(null);
      }, STT_TIMEOUT_MS);
      pendingSttRequests.set(requestId, (ok, text) => {
        clearTimeout(timer);
        resolve(ok ? text : null);
      });
      sendControl("stt", { audioBase64, format, requestId });
    });
  }

  // onDone (if given) fires once this specific audio finishes/errors/fails
  // to start -- how playOneSpeech knows to advance the speech queue.
  function playAudioBase64(base64, entry, onDone) {
    const audio = new Audio(`data:audio/mp3;base64,${base64}`);
    if (entry) {
      if (entry.aborted) { onDone?.(); return audio; } // stopped while the request was in flight
      entry.audio = audio;
      const clear = () => {
        if (activeSpeech === entry) activeSpeech = null;
        entry.btn?.classList.remove("speaking");
        hideVoiceControls();
        onDone?.();
      };
      audio.addEventListener("ended", clear);
      audio.addEventListener("error", clear);
      audio.addEventListener("play", () => { if (activeSpeech === entry) showVoiceControls(); });
      audio.addEventListener("pause", () => { if (activeSpeech === entry) updateVoicePauseBtnLabel(); });
    }
    audio.play().catch(() => { onDone?.(); });
    return audio;
  }

  function addStatusLine(text) {
    const div = document.createElement("div");
    div.className = "status-line";
    div.textContent = text;
    messagesEl.appendChild(div);
    scrollToEnd();
    return div;
  }

  function addBanner(text) {
    setStatusBarText(text, 6000);
  }

  function setToolStatus(text) {
    setStatusBarText(text || lastConnText);
  }

  // A ticking "still working" indicator, independent of anything the model
  // itself says. Confirmed this can't be fixed by prompting the model to
  // "narrate progress every few seconds" -- while a tool call is in flight,
  // the model isn't being invoked at all (it already emitted the tool_use
  // block and is simply waiting for the result), so there is no token
  // generation happening for it to narrate with. Only the UI itself can
  // show that time is passing during that gap, hence a local timer instead
  // of a system-prompt instruction.
  let heartbeatTimer = null;
  let heartbeatStartedAt = null;
  let heartbeatToolName = null;

  // Confirmed live (2026-09-03) as a real, reachable bug, not hypothetical: turnQueue's
  // busy-tracking assumes every queued turn ALWAYS eventually gets exactly one matching
  // WS notification (a "result", or the "stopped" handler's own placeholder-swap) to shift/
  // clear it -- there's at least one real race (rapid repeated Stop clicks landing while the
  // backend's own turnPending bookkeeping is mid-flip) where that assumption breaks and NO
  // such notification ever arrives. When that happens there is no other recovery path at
  // all: Stop becomes a permanent no-op (the backend's own stop() is itself a no-op once its
  // turnPending is already false), and the input stays "busy" forever with no way to unstick
  // it apart from restarting the whole app. lastTurnActivityAt/STUCK_TURN_THRESHOLD_MS below
  // are a client-only, last-resort safety net for exactly that: independent of WHY a
  // notification got lost, if nothing has moved for this long while still marked busy, just
  // recover locally instead of staying stuck indefinitely.
  let lastTurnActivityAt = null;
  const STUCK_TURN_THRESHOLD_MS = 180_000;

  function touchTurnActivity() {
    lastTurnActivityAt = Date.now();
  }

  function recoverFromStuckTurn() {
    console.error(`chat.js: no turn activity for >${STUCK_TURN_THRESHOLD_MS}ms while busy -- self-healing stuck UI state`);
    turnQueue = [];
    setBusy(false);
    stopHeartbeat();
    addBanner("Не дождались ответа от Кэролайн вовремя — поле ввода разблокировано. Начатое действие могло не завершиться.");
  }

  function updateHeartbeatText() {
    const elapsed = Math.max(0, Math.round((Date.now() - heartbeatStartedAt) / 1000));
    const activity = heartbeatToolName ? `🔧 ${heartbeatToolName}…` : "Working…";
    setToolStatus(`${activity} (${elapsed}s)`);
    if (lastTurnActivityAt !== null && Date.now() - lastTurnActivityAt > STUCK_TURN_THRESHOLD_MS) {
      recoverFromStuckTurn();
    }
  }

  function startHeartbeat() {
    touchTurnActivity();
    if (heartbeatTimer) return; // already running -- don't reset the elapsed clock
    heartbeatStartedAt = Date.now();
    updateHeartbeatText();
    heartbeatTimer = setInterval(updateHeartbeatText, 5000);
  }

  function stopHeartbeat() {
    if (heartbeatTimer) clearInterval(heartbeatTimer);
    heartbeatTimer = null;
    heartbeatToolName = null;
    lastTurnActivityAt = null;
    setToolStatus("");
  }

  // Tracked separately from voiceSession so updateStopBtnVisibility() can
  // OR the two together -- stopBtn needs to show for either "a turn is
  // running" or "voice input is live", not just the first one (see
  // toggleVoiceRecording/stopVoiceSession for where voice recording toggles
  // this too).
  let turnBusy = false;

  function updateStopBtnVisibility() {
    const show = turnBusy || !!voiceSession;
    stopBtn.style.display = show ? "" : "none";
    // Sending stays enabled regardless while just a turn is busy -- the
    // backend queues a follow-up and runs it right after, so there's no
    // need to block typing/sending. The label/behavior below just clarifies
    // which thing this click will actually cancel.
    stopBtn.title = voiceSession ? "Cancel voice input" : "Stop";
  }

  function setBusy(busy) {
    turnBusy = busy;
    updateStopBtnVisibility();
    updateBackendLamp();
  }

  function connect() {
    ws = new WebSocket(`ws://127.0.0.1:${port}/?tab=${encodeURIComponent(tabId)}`);

    ws.addEventListener("open", () => {
      wsConnected = true;
      setStatus("connected", "connected");
      pollChannelStatus();
      sendControl("visual_mode_get");
      // See replayTranscript()/history.ts: an empty localStorage transcript
      // doesn't necessarily mean no history exists -- it might just be a
      // fresh origin (e.g. after the file:// -> https://caroline.local
      // move). Ask the backend to rebuild it from the real session
      // transcript. Guarded so a reconnect never re-requests/re-replays it.
      if (!historyRequested) {
        historyRequested = true;
        if (loadTranscript().length === 0) sendControl("get_history", {});
      }
    });
    ws.addEventListener("close", () => {
      wsConnected = false;
      setStatus("reconnecting…", "restarting");
      // Whatever was in flight lost its connection to the backend that was
      // running it -- no "result" (or "stopped") is coming for it anymore.
      // Without this, the Stop button and busy state could get stuck
      // showing "something is running" indefinitely after a connection drop
      // that had nothing to do with the user's own turnQueue bookkeeping.
      turnQueue = [];
      setBusy(false);
      stopHeartbeat();
      setTimeout(connect, 1500);
    });
    ws.addEventListener("error", () => {
      wsConnected = false;
      setStatus("connection error", "error");
    });
    ws.addEventListener("message", (ev) => {
      let evt;
      try { evt = JSON.parse(ev.data); } catch { return; }
      handleEvent(evt);
    });
  }

  function sendControl(op, extra) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type: "control_request", op, ...extra }));
  }

  function handleEvent(evt) {
    if (evt.type === "control_stream") {
      if (evt.op === "auth_login") {
        authLoginOutput.style.display = "block";
        authLoginOutput.textContent += evt.chunk;
      }
      return;
    }

    if (evt.type === "control_response") {
      handleControlResponse(evt);
      return;
    }

    if (evt.type === "open_editor" || evt.type === "open_office_editor" || evt.type === "close_editor" || evt.type === "open_login" || evt.type === "open_payment") {
      // Relayed to the WPF shell over WebView2's native messaging bridge --
      // it owns the actual floating window(s) (see MainWindow.xaml.cs's
      // WebMessageReceived handler and DocumentViewerWindow). If there's no
      // native host at all (e.g. this page loaded in a plain browser during
      // development), fail an open_editor/open_login's tool call immediately
      // instead of leaving nothing to ever report it closed; close_editor
      // with nothing open to close has nothing further to report either way.
      if (window.chrome?.webview) {
        window.chrome.webview.postMessage(evt);
      } else if (evt.type === "open_editor" || evt.type === "open_office_editor") {
        sendControl("editor_result", { requestId: evt.requestId, outcome: "error", path: evt.path, message: "No native host available." });
      } else if (evt.type === "open_login") {
        sendControl("login_submit", { requestId: evt.requestId, cancelled: true });
      }
      return;
    }

    if (evt.type === "visual_mode_config") {
      // One-time push at Caroline's own startup (see server.ts's OutEvent doc
      // comment) -- just relayed to the native host so it can warm up the
      // right model in the background; the page's own visualModeState comes
      // from visual_mode_get (sent right after this on the same connection).
      if (window.chrome?.webview) window.chrome.webview.postMessage(evt);
      return;
    }

    if (evt.type === "proactive_turn_queued") {
      // A turn this page never called send() for (reminder, ratatosk nudge, startup
      // greeting, etc.) is about to produce its own "result" -- push a placeholder so
      // the "result" handler's turnQueue.shift() stays aligned 1:1 with the backend's
      // actual turn count. Without this, that result silently steals whatever real
      // queued turn happens to be at the front (confirmed live 2026-09-03: a memory-
      // backup reminder landing between a voice message and its reply ate the voice
      // turn's queue slot, so isVoice got lost and its TTS never fired).
      turnQueue.push({ isVoice: false, assistantText: "" });
      return;
    }

    if (evt.type === "system_notice") {
      // Not part of a real SDK turn -- see server.ts's OutEvent doc comment for
      // system_notice. Per explicit instruction: service/system messages like
      // this must NEVER show as a chat bubble -- status bar only. cls defaults
      // to "error" (red lamp, for a depleted balance -- something the user can
      // actually fix) when the backend omits it; a usage/session-window limit
      // sends "restarting" instead (yellow, same as a plain restart) since it
      // resets on its own and there's nothing to fix. Also unsticks the UI the
      // same way a "restarting" caroline_status would, since no "result" is
      // ever coming for whatever turn was in flight when this fired.
      setStatus(evt.text, evt.cls || "error");
      turnQueue = [];
      setBusy(false);
      stopHeartbeat();
      return;
    }

    if (evt.type === "caroline_status") {
      if (evt.status === "connected") setStatus("connected", "connected");
      else if (evt.status === "restarting") {
        // Status bar only, no chat bubble -- per explicit instruction
        // (2026-09-04), same reasoning as system_notice just above: a
        // session restart (whatever caused it -- a real hang, or an hourly
        // compaction cycle switching resume targets) is service-level
        // noise, not a message from Caroline. It used to also post a
        // "Session hiccup" line straight into the dialog, which got
        // especially repetitive once compaction started restarting the
        // session roughly once an hour on its own.
        setStatus("recovering session…", "restarting");
        turnQueue = []; // the session restarting means none of these will get their own "result"
        setBusy(false);
        stopHeartbeat();
      } else if (evt.status === "restart_backoff") {
        // Repeated failures with no actionable cause (not billing, not a rate
        // limit -- those go through "error"/system_notice instead) -- must
        // never escalate to a blocking dialog: a blocking panel is only for
        // conditions the user can actually do something about (e.g. a
        // depleted balance). Same calm "recovering session…" text as a plain
        // restart -- per explicit instruction (2026-09-09), superseding an
        // earlier decision (2026-09-05) to show the raw "failed N times in
        // Mmin" detail here: confirmed live that surfacing the actual retry
        // count/backoff timing to the user is alarming on its own, even
        // though the condition itself is never dangerous or blocking. The
        // detail still reaches caroline.log in full via setConnState's own
        // logging -- only the user-facing text changed.
        setStatus("recovering session…", "restarting");
        turnQueue = [];
        setBusy(false);
        stopHeartbeat();
      } else if (evt.status === "stopped") {
        turnQueue.shift(); // the interrupted turn won't get a "result" of its own
        // The backend immediately injects a synthetic message telling Caroline
        // she was stopped, which runs as its own turn ahead of anything the
        // user queued -- a placeholder here keeps turnQueue aligned with the
        // backend's actual turn count so the *next real* turn's result isn't
        // misattributed to it.
        turnQueue.unshift({ isVoice: false, assistantText: "" });
        setBusy(true);
        stopHeartbeat();
        startHeartbeat();
      }
      return;
    }

    if (evt.type !== "sdk_message") return;
    const msg = evt.message;

    if (msg.type === "assistant") {
      touchTurnActivity();
      if (turnQueue.length === 0) {
        // Activity for a turn this page never called send() for -- it was
        // already running on the backend before this page (re)connected
        // (app restart, or a reconnect after a dropped connection), so
        // neither setBusy(true) nor startHeartbeat() ever ran for it.
        // Without this, the heartbeat clock reads elapsed time from a null
        // start (Date.now() - null -- a huge bogus second count) and Stop
        // never appears, since only setBusy(true) shows it.
        turnQueue.push({ isVoice: false, assistantText: "" });
        setBusy(true);
        startHeartbeat();
      }
      const turn = turnQueue[0]; // oldest still-unresolved turn -- see turnQueue's doc comment
      const blocks = msg.message.content || [];
      // A message that also contains a tool_use block is, by construction, NOT
      // the turn's final answer -- one SDK "assistant" message is one API
      // round-trip with a single stop_reason, so any text alongside a tool_use
      // is pre-tool narration ("let me check X", agentic self-talk, sometimes
      // even in the wrong language) rather than something addressed to the
      // user. Only a message with no tool_use (the turn actually ending) is
      // shown as a chat bubble -- confirmed live 2026-09-08: narration bubbles
      // like "that overwrote the whole note, let me redo it" were leaking into
      // the chat as if Caroline were talking to the user.
      const hasToolUse = blocks.some((b) => b.type === "tool_use");
      for (const block of blocks) {
        if (block.type === "text" && block.text) {
          // A reminder/proactive check that found nothing worth surfacing
          // (see policies.ts's noUpdateSentinelInstruction) -- suppress just
          // the bubble; tool calls/heartbeat/turn bookkeeping in this same
          // loop still proceed normally, only the text block is skipped.
          if (block.text.trim() === "[[NO_UPDATE]]") continue;
          if (hasToolUse) continue;
          addBubble("assistant", block.text);
          if (turn) turn.assistantText += (turn.assistantText ? "\n\n" : "") + block.text;
        } else if (block.type === "tool_use") {
          // Confirmed live: a long turn chaining many tool calls (browsing,
          // clicking, checking folders...) showed "Bash... (12660s)" -- 3.5h
          // attributed to a single Bash call that had actually just started,
          // because heartbeatStartedAt only ever reset at the TURN's start
          // (startHeartbeat's own guard), not per individual tool call. The
          // elapsed clock now restarts here too, so it reflects time in the
          // CURRENT tool, matching what the label actually claims.
          heartbeatToolName = block.name;
          heartbeatStartedAt = Date.now();
          updateHeartbeatText();
        }
      }
    } else if (msg.type === "result") {
      const turn = turnQueue.shift();
      setBusy(turnQueue.length > 0);
      if (turnQueue.length === 0) stopHeartbeat();
      else { heartbeatToolName = null; updateHeartbeatText(); } // another queued turn is about to start
      if (msg.subtype === "error") {
        addBanner("Something went wrong processing that message.");
      } else if (evt.isVoice && turn && turn.assistantText) {
        // isVoice comes from the backend's own result event (see server.ts's
        // OutEvent doc comment), NOT from turnQueue -- turnQueue is still used
        // for assistantText accumulation/busy-state, but a proactive (backend-
        // only) turn landing between this turn's submit and its result can
        // desync turnQueue's isVoice specifically (confirmed live 2026-09-03:
        // this is why voice replies silently stopped being spoken). The
        // backend's own turnIsVoice is authoritative and immune to that.
        speak(turn.assistantText);
      }
    }
  }

  function handleControlResponse(evt) {
    if (evt.op === "get_history") {
      if (evt.ok) {
        try {
          const entries = JSON.parse(evt.stdout || "[]");
          for (const entry of entries) {
            const div = addBubble(entry.role, entry.text, [], { persist: true, ts: entry.ts });
            const filePath = extractDehydratedFilePath(entry.text);
            if (filePath) addExpandLink(div, filePath);
          }
          if (entries.length > 0) addStatusLine("— earlier conversation restored —");
        } catch { /* leave the chat empty rather than show garbage */ }
      }
    } else if (evt.op === "auth_status") {
      try {
        const s = JSON.parse(evt.stdout || "{}");
        authStatusText.textContent = s.loggedIn
          ? `Logged in as ${s.email || "?"} (${s.subscriptionType || s.apiProvider || "unknown plan"})`
          : "Not logged in.";
      } catch {
        authStatusText.textContent = evt.ok ? "Status unavailable." : (evt.stderr || "Error checking status.");
      }
    } else if (evt.op === "auth_login") {
      authLoginOutput.textContent += evt.ok ? "\n[done]\n" : "\n[failed]\n";
      sendControl("auth_status");
      sendControl("mode_get");
    } else if (evt.op === "auth_logout") {
      sendControl("auth_status");
      sendControl("mode_get");
    } else if (evt.op === "mode_get") {
      try {
        const m = JSON.parse(evt.stdout || "{}");
        const label = {
          "own-anthropic-oauth": "Your own Claude subscription (logged in above)",
          "own-anthropic-key": "Your own Anthropic API key (pasted below)",
          "sw-proxy": "SquirrelWisdom subscription (metered)",
          "none": "None configured — chat will fail until you set one up below",
        }[m.chatSource] || m.chatSource;
        modeStatusText.textContent = `Chat is currently paid by: ${label}`;
        lastChatSource = m.chatSource || null;
      } catch {
        modeStatusText.textContent = "Status unavailable.";
        lastChatSource = null;
      }
      renderSwUpsellHint();
    } else if (evt.op === "sw_status") {
      try {
        const s = JSON.parse(evt.stdout || "{}");
        if (!s.loggedIn) {
          swAccountStatus.textContent = "Not logged in to SquirrelWisdom.";
        } else if (s.balanceError) {
          swAccountStatus.textContent = `Logged in as ${s.email}. Balance check failed: ${s.balanceError}`;
        } else {
          swAccountStatus.textContent = `Logged in as ${s.email}. Balance: ${s.balancePia} PIA.`;
        }
        lastSwLoggedIn = !!s.loggedIn;
      } catch {
        swAccountStatus.textContent = "Status unavailable.";
        lastSwLoggedIn = null;
      }
      renderSwUpsellHint();
    } else if (evt.op === "ratatosk_status_get") {
      try {
        const s = JSON.parse(evt.stdout || "{}");
        const ownerLine = s.ownerEmail ? `You: ${s.ownerEmail}` : "You: not logged in to SquirrelWisdom.";
        const carolineLine = s.carolineEmail ? `Caroline: ${s.carolineEmail}` : "Caroline: no account registered yet.";
        ratatoskStatusText.textContent = `${ownerLine}  |  ${carolineLine}`;
        ratatoskRegisterBtn.disabled = !!s.carolineEmail;
        ratatoskRegisterBtn.textContent = s.carolineEmail ? "Registered" : "Register Caroline's own account";
      } catch {
        ratatoskStatusText.textContent = "Status unavailable.";
      }
    } else if (evt.op === "ratatosk_own_account_register") {
      if (!evt.ok) addBanner(`Could not register Caroline's Ratatosk account: ${evt.stderr || "unknown error"}`);
      sendControl("ratatosk_status_get");
    } else if (evt.op === "ratatosk_channel_status") {
      try {
        updateChannelLamp(evt.ok ? JSON.parse(evt.stdout || "{}") : null);
      } catch {
        updateChannelLamp(null);
      }
    } else if (evt.op === "own_anthropic_key_get") {
      try {
        const s = JSON.parse(evt.stdout || "{}");
        ownAnthropicKeyStatus.textContent = s.isSet ? "A key is currently saved." : "No key saved.";
      } catch {
        ownAnthropicKeyStatus.textContent = "";
      }
    } else if (evt.op === "own_anthropic_key_set") {
      ownAnthropicKeyInput.value = "";
      sendControl("own_anthropic_key_get");
      sendControl("mode_get");
    } else if (evt.op === "sms_account_get") {
      try {
        const s = JSON.parse(evt.stdout || "{}");
        if (s.error) {
          smsAccountStatus.textContent = `Status unavailable: ${s.error}`;
        } else if (s.hasAccount) {
          smsAccountStatus.textContent = s.sender ? `Account registered. Sender: ${s.sender}.` : "Account registered (shared SMTP2GO number).";
        } else {
          smsAccountStatus.textContent = "No SMTP2GO account registered.";
        }
      } catch {
        smsAccountStatus.textContent = "Status unavailable.";
      }
    } else if (evt.op === "sms_account_set") {
      if (!evt.ok) addBanner(`Could not save SMS account: ${evt.stderr || "unknown error"}`);
      smsApiKeyInput.value = "";
      smsSenderInput.value = "";
      sendControl("sms_account_get");
    } else if (evt.op === "sms_account_remove") {
      if (!evt.ok) addBanner(`Could not remove SMS account: ${evt.stderr || "unknown error"}`);
      sendControl("sms_account_get");
    } else if (evt.op === "login_submit") {
      sendControl("mode_get");
      sendControl("sw_status");
    } else if (evt.op === "open_login_from_settings") {
      // Nothing to do here -- the actual login form opens via the
      // server-initiated "open_login" event this triggers (see chat.js's
      // open_login handler below), same as ensure_squirrelwisdom_login.
    } else if (evt.op === "mcp_list") {
      renderMcpList(evt.stdout || evt.stderr || "");
    } else if (evt.op === "mcp_add" || evt.op === "mcp_remove") {
      if (!evt.ok) addBanner(`MCP server change failed: ${evt.stderr || evt.stdout || "unknown error"}`);
      sendControl("mcp_list");
    } else if (evt.op === "persona_get") {
      try {
        const { edit } = JSON.parse(evt.stdout || "{}");
        personaProfile.value = edit.profileKey || "custom";
        if (edit.profileKey === "custom") {
          personaName.value = edit.custom.name || "";
          personaGender.value = edit.custom.gender || "";
          personaAge.value = edit.custom.age || "";
          personaBio.value = edit.custom.bio || "";
          personaBiography.value = "";
          personaPhotosDir.value = "";
        } else {
          const o = (edit.overrides && edit.overrides[edit.profileKey]) || {};
          personaName.value = o.name || "";
          personaGender.value = o.gender || "";
          personaAge.value = o.age || "";
          personaBio.value = o.bio || "";
          personaBiography.value = o.biography || "";
          personaPhotosDir.value = o.photosDir || "";
        }
        updatePersonaFieldsVisibility();
      } catch { /* leave fields as-is */ }
    } else if (evt.op === "persona_set") {
      if (!evt.ok) addBanner(`Could not save personality: ${evt.stderr || "unknown error"}`);
    } else if (evt.op === "persona_reset") {
      if (!evt.ok) addBanner(`Could not reset profile: ${evt.stderr || "unknown error"}`);
      else sendControl("persona_get");
    } else if (evt.op === "visual_mode_get") {
      try {
        const v = JSON.parse(evt.stdout || "{}");
        visualModeState = { enabled: !!v.enabled, available: !!v.available };
        visualModeEnabled.checked = !!v.enabled;
        // Per explicit instruction: visible but disabled for a custom profile, not hidden.
        visualModeEnabled.disabled = !v.available;
        visualModeUnavailableHint.style.display = v.available ? "none" : "block";
      } catch { /* leave as-is */ }
    } else if (evt.op === "tts") {
      const cb = pendingTtsRequests.get(evt.requestId);
      pendingTtsRequests.delete(evt.requestId);
      cb?.(evt.ok, evt.stdout, evt.stderr);
    } else if (evt.op === "stt") {
      const cb = pendingSttRequests.get(evt.requestId);
      pendingSttRequests.delete(evt.requestId);
      cb?.(evt.ok, evt.stdout, evt.stderr);
      if (!evt.ok) addBanner(`Could not transcribe audio: ${evt.stderr || "unknown error"}`);
    } else if (evt.op === "open_file") {
      if (!evt.ok) addBanner(`Could not open that file: ${evt.stderr || "unknown error"}`);
    } else if (evt.op === "expand_dehydrated_ref") {
      const cb = pendingExpandRequests.get(evt.requestId);
      pendingExpandRequests.delete(evt.requestId);
      let parsed = null;
      try { parsed = evt.ok ? JSON.parse(evt.stdout || "null") : null; } catch { parsed = null; }
      cb?.(evt.ok, parsed, evt.stderr);
    }
  }

  messagesEl.addEventListener("click", (e) => {
    const link = e.target.closest(".doc-link");
    if (!link) return;
    e.preventDefault();
    sendControl("open_file", { path: link.dataset.path });
  });

  // "name: command args - status" (one per line, from `claude mcp list`) ->
  // a short "name — status" listing. Keeps the raw command out of the
  // read-only view (it's internal paths, not useful to read) while the
  // add/remove forms below still work with the full command directly.
  function renderMcpList(raw) {
    mcpListOutput.innerHTML = "";
    const lines = raw.split("\n").map((l) => l.trim()).filter(Boolean);
    let any = false;
    for (const line of lines) {
      const m = line.match(/^([^\s:][^:]*):\s*.+?-\s*(✔?\s*Connected|⏸?\s*Pending[^,]*|.+)$/);
      if (!m) continue;
      any = true;
      const row = document.createElement("div");
      row.textContent = `${m[1].trim()}  —  ${m[2].trim()}`;
      mcpListOutput.appendChild(row);
    }
    if (!any) mcpListOutput.textContent = raw || "(no servers)";
  }

  function openSettings() {
    settingsOverlay.classList.add("open");
    authLoginOutput.textContent = "";
    authLoginOutput.style.display = "none";
    sendControl("auth_status");
    sendControl("mcp_list");
    sendControl("persona_get");
    sendControl("visual_mode_get");
    sendControl("mode_get");
    sendControl("sw_status");
    sendControl("own_anthropic_key_get");
    sendControl("ratatosk_status_get");
    sendControl("sms_account_get");
  }

  ratatoskRegisterBtn.addEventListener("click", () => {
    ratatoskRegisterBtn.disabled = true;
    ratatoskRegisterBtn.textContent = "Registering…";
    sendControl("ratatosk_own_account_register");
  });

  ownAnthropicKeySaveBtn.addEventListener("click", () => {
    const key = ownAnthropicKeyInput.value.trim();
    if (!key) return;
    sendControl("own_anthropic_key_set", { anthropicApiKey: key });
  });
  ownAnthropicKeyClearBtn.addEventListener("click", () => {
    sendControl("own_anthropic_key_set", { anthropicApiKey: null });
  });
  smsAccountSaveBtn.addEventListener("click", () => {
    const key = smsApiKeyInput.value.trim();
    if (!key) return;
    sendControl("sms_account_set", { smtp2goApiKey: key, smtp2goSender: smsSenderInput.value.trim() || null });
  });
  smsAccountRemoveBtn.addEventListener("click", () => {
    sendControl("sms_account_remove");
  });
  swLoginBtn.addEventListener("click", () => {
    sendControl("open_login_from_settings");
  });
  swTopUpBtn.addEventListener("click", () => {
    sendControl("open_payment_from_settings");
  });

  personaSaveBtn.addEventListener("click", () => {
    const profileKey = personaProfile.value;
    const persona = profileKey === "custom"
      ? {
        profileKey,
        name: personaName.value.trim() || "Caroline",
        gender: personaGender.value.trim() || "female",
        age: personaAge.value.trim() || "middle-aged",
        bio: personaBio.value.trim(),
      }
      : {
        // Empty fields here mean "no override, keep the standard profile's
        // own value" (see persona.ts's getPersona merge) -- unlike custom,
        // these must NOT fall back to Caroline-shaped defaults.
        profileKey,
        name: personaName.value.trim() || undefined,
        gender: personaGender.value.trim() || undefined,
        age: personaAge.value.trim() || undefined,
        bio: personaBio.value.trim() || undefined,
        biography: personaBiography.value.trim() || undefined,
        photosDir: personaPhotosDir.value.trim() || undefined,
      };
    sendControl("persona_set", { persona });
    personaSaveBtn.textContent = "Saved ✓ (applies on next session restart)";
    setTimeout(() => { personaSaveBtn.textContent = "Save personality"; }, 3000);
  });

  personaResetBtn.addEventListener("click", () => {
    if (personaProfile.value === "custom") return;
    sendControl("persona_reset", { profileKey: personaProfile.value });
  });

  function closeSettings() {
    settingsOverlay.classList.remove("open");
  }

  settingsBtn.addEventListener("click", openSettings);
  closeSettingsBtn.addEventListener("click", closeSettings);
  settingsOverlay.addEventListener("click", (e) => {
    if (e.target === settingsOverlay) closeSettings();
  });

  loginBtn.addEventListener("click", () => {
    authLoginOutput.textContent = "";
    authLoginOutput.style.display = "block";
    sendControl("auth_login");
  });
  logoutBtn.addEventListener("click", () => sendControl("auth_logout"));
  mcpRefreshBtn.addEventListener("click", () => sendControl("mcp_list"));
  mcpAddBtn.addEventListener("click", () => {
    const name = document.getElementById("mcpName").value.trim();
    const command = document.getElementById("mcpCommand").value.trim();
    const args = document.getElementById("mcpArgs").value.trim().split(/\s+/).filter(Boolean);
    if (!name || !command) return;
    sendControl("mcp_add", { name, command, args });
  });
  mcpRemoveBtn.addEventListener("click", () => {
    const name = document.getElementById("mcpRemoveName").value.trim();
    if (!name) return;
    sendControl("mcp_remove", { name });
  });

  const MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024; // 20MB, matches typical Claude API per-file limits

  function readFileAsBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(reader.error || new Error("FileReader error"));
      reader.onloadend = () => {
        const result = String(reader.result || "");
        resolve(result.includes(",") ? result.split(",")[1] : result);
      };
      reader.readAsDataURL(file);
    });
  }

  function renderPendingAttachments() {
    attachmentsEl.innerHTML = "";
    pendingAttachments.forEach((a, i) => {
      const chip = document.createElement("div");
      chip.className = "attachment-chip";
      const iconHtml = a.mimeType.startsWith("image/")
        ? `<img class="attachment-thumb" src="data:${a.mimeType};base64,${a.dataBase64}" alt="" />`
        : `<span>${a.mimeType.startsWith("video/") ? "🎬" : "📄"}</span>`;
      chip.innerHTML = `${iconHtml}<span class="name"></span><button title="Remove">✕</button>`;
      chip.querySelector(".name").textContent = a.name;
      chip.querySelector("button").addEventListener("click", () => {
        pendingAttachments.splice(i, 1);
        renderPendingAttachments();
      });
      attachmentsEl.appendChild(chip);
    });
  }

  async function handleFilesSelected(files) {
    for (const file of files) {
      if (file.size > MAX_ATTACHMENT_BYTES) {
        addBanner(`"${file.name}" is too large (max 20MB) — skipped.`);
        continue;
      }
      try {
        const dataBase64 = await readFileAsBase64(file);
        pendingAttachments.push({
          name: file.name,
          mimeType: file.type || "application/octet-stream",
          dataBase64,
        });
      } catch (err) {
        addBanner(`Could not read "${file.name}": ${err.message || err}`);
      }
    }
    renderPendingAttachments();
  }

  attachBtn.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => {
    if (fileInput.files.length) handleFilesSelected(fileInput.files);
    fileInput.value = "";
  });

  // Paste an image/file (Ctrl+V) -- only intercepted when the clipboard
  // actually carries files; a plain text paste (the common case, into
  // inputEl) falls through untouched so normal typing/pasting keeps working.
  document.addEventListener("paste", (e) => {
    const files = e.clipboardData?.files;
    if (files && files.length) {
      e.preventDefault();
      handleFilesSelected(files);
    }
  });

  // Drag-and-drop anywhere in the window. Both dragover and drop need
  // preventDefault -- without it on dragover, the browser refuses the drop
  // entirely and just shows the "not allowed" cursor; without it on drop,
  // it navigates the page to the dropped file instead of handling it here.
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => {
    e.preventDefault();
    if (e.dataTransfer?.files?.length) handleFilesSelected(e.dataTransfer.files);
  });

  function send(voiceOrigin, overrideText) {
    const text = (overrideText !== undefined ? overrideText : inputEl.value).trim();
    if (!text && pendingAttachments.length === 0) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      // Previously a silent no-op -- looked exactly like a hung/broken UI
      // (typed text, clicked send, nothing visibly happened) instead of
      // what was actually true: not connected to the backend right now.
      addBanner("Not connected to Caroline's backend right now — message not sent. It'll reconnect automatically; try again in a moment.");
      return;
    }
    // No busy-gate: the backend queues this behind whatever's currently
    // running and processes it next, so there's no reason to make the user
    // wait for the previous turn just to queue up the next one.
    turnQueue.push({ isVoice: !!voiceOrigin, assistantText: "" });
    addBubble("user", text, pendingAttachments);
    ws.send(JSON.stringify({ type: "user_message", text, attachments: pendingAttachments, voice: !!voiceOrigin }));
    inputEl.value = "";
    pendingAttachments = [];
    renderPendingAttachments();
    autoGrow();
    setBusy(true);
    startHeartbeat();
  }

  function stopCurrentTurn() {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type: "interrupt" }));
  }

  // --- Voice input: record until silence (or manual stop), then send ---
  // VAD (adaptive noise-floor + RMS threshold) ported from DictateWin's
  // AudioRecorder.cs -- but NOT its "continuous" mode: here, silence ends
  // the whole recording and sends it, same as a manual stop click, rather
  // than closing just the current segment and starting a fresh one while
  // staying live. Confirmed this is what was wanted -- one utterance, one
  // send, done, not an open-ended continuous dictation stream.
  const VOICE_PREFERRED_MIME_TYPES = [
    "audio/ogg;codecs=opus", "audio/ogg", "audio/webm;codecs=opus", "audio/webm", "audio/wav",
  ];
  // Same shape as DictateWin's AudioRecorder constants.
  const VAD_NOISE_FLOOR_ALPHA = 0.05;
  const VAD_SPEECH_MULTIPLIER = 4.0;
  const VAD_MIN_THRESHOLD = 0.0003;
  const VAD_SILENCE_TIMEOUT_MS = 900;

  let voiceSession = null; // non-null for the whole live session (start click -> stop+send)

  function voiceMimeToFormat(mime) {
    const m = (mime || "").toLowerCase();
    if (m.includes("ogg")) return "ogg";
    if (m.includes("webm")) return "webm";
    if (m.includes("wav")) return "wav";
    return "bin";
  }

  function setMicState(state) {
    micBtn.classList.toggle("recording", state === "recording");
    micBtn.classList.toggle("transcribing", state === "transcribing");
    micBtn.title = state === "recording" ? "Recording… click to stop"
      : state === "transcribing" ? "Transcribing…" : "Voice input";
  }

  function createVoiceRecorder(session) {
    const recorder = session.chosenMime
      ? new MediaRecorder(session.stream, { mimeType: session.chosenMime })
      : new MediaRecorder(session.stream);
    const effectiveMime = recorder.mimeType || session.chosenMime || "audio/webm";
    const chunks = [];
    recorder.ondataavailable = (e) => { if (e?.data?.size) chunks.push(e.data); };
    recorder.onstop = () => {
      processRecording(session, new Blob(chunks, { type: effectiveMime }), effectiveMime);
    };
    recorder.onerror = () => {
      addBanner("Recording error.");
      stopVoiceSession(session, true);
    };
    return recorder;
  }

  async function processRecording(session, blob, mime) {
    if (session !== voiceSession) return; // a newer session already replaced this one
    setMicState("transcribing");
    try {
      if (blob.size > 0) {
        const audioBase64 = await readFileAsBase64(blob);
        const text = await transcribe(audioBase64, voiceMimeToFormat(mime));
        if (session.cancelled) {
          // Cancelled while STT was in flight -- discard the result even
          // though the network round trip itself already completed.
        } else if (text && text.trim()) {
          if (voiceAutoSend.checked) {
            send(true, text.trim());
          } else {
            inputEl.value = inputEl.value ? `${inputEl.value} ${text.trim()}` : text.trim();
            autoGrow();
            inputEl.focus();
          }
        }
      }
    } catch (err) {
      // Never leave the mic stuck on "transcribing" over a failed recording --
      // confirmed live as a real failure mode (a stalled STT round trip left
      // the button red indefinitely, with no way to recover short of
      // restarting the whole app).
      addBanner(`Voice input failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      try { session.stream.getTracks().forEach((t) => t.stop()); } catch {}
      try { session.audioCtx.close(); } catch {}
      if (voiceSession === session) voiceSession = null;
      setMicState("idle");
      updateStopBtnVisibility();
      resumeSpeechIfPaused();
    }
  }

  // Runs only while the session hasn't been stopped yet -- stopVoiceSession
  // cancels this loop's pending frame immediately, so there's no need for
  // this function to check session.stopping itself.
  function runVadFrame(session) {
    session.analyser.getFloatTimeDomainData(session.vadData);
    let sum = 0;
    for (let i = 0; i < session.vadData.length; i++) sum += session.vadData[i] * session.vadData[i];
    const rms = Math.sqrt(sum / session.vadData.length);
    const threshold = Math.max(session.noiseFloor * VAD_SPEECH_MULTIPLIER, VAD_MIN_THRESHOLD);

    if (rms > threshold) {
      session.speechDetected = true;
      session.silenceStartAt = null;
    } else if (!session.speechDetected) {
      // Only adapt the floor while apparently not mid-utterance -- matches
      // DictateWin's own reasoning, so a loud sustained voice doesn't drag
      // the floor up and make the VAD deaf to quieter follow-up speech.
      session.noiseFloor = session.noiseFloor * (1 - VAD_NOISE_FLOOR_ALPHA) + rms * VAD_NOISE_FLOOR_ALPHA;
    } else {
      if (session.silenceStartAt === null) {
        session.silenceStartAt = performance.now();
      } else if (performance.now() - session.silenceStartAt > VAD_SILENCE_TIMEOUT_MS) {
        stopVoiceSession(session, false); // silence = stop and send, same as a manual click
        return;
      }
    }
    session.rafId = requestAnimationFrame(() => runVadFrame(session));
  }

  function stopVoiceSession(session, discard) {
    session.stopping = true;
    if (session.rafId) cancelAnimationFrame(session.rafId);
    if (discard) {
      session.cancelled = true; // see processRecording's post-transcribe check
      try { session.recorder?.state === "recording" && session.recorder.stop(); } catch {}
      try { session.stream.getTracks().forEach((t) => t.stop()); } catch {}
      try { session.audioCtx.close(); } catch {}
      if (voiceSession === session) voiceSession = null;
      setMicState("idle");
      updateStopBtnVisibility();
      resumeSpeechIfPaused();
      return;
    }
    if (session.recorder.state === "recording") session.recorder.stop();
    else processRecording(session, new Blob([]), "audio/webm");
  }

  async function toggleVoiceRecording() {
    if (voiceSession) {
      stopVoiceSession(voiceSession, false);
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia) {
      addBanner("Microphone is not supported in this browser.");
      return;
    }

    let stream;
    try {
      // Browsers enable auto gain control / noise suppression by default,
      // which actively normalizes quiet audio toward a target level -- that
      // fights a simple RMS-threshold VAD by keeping background noise
      // artificially close to speech level during pauses, so real silence
      // never reads as clearly quieter. Disabled so the VAD sees closer to
      // the raw signal DictateWin's own WASAPI-capture-based version assumes.
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { autoGainControl: false, noiseSuppression: false, echoCancellation: false },
      });
    } catch {
      addBanner("Microphone access denied.");
      return;
    }

    let chosenMime = null;
    for (const t of VOICE_PREFERRED_MIME_TYPES) {
      try { if (window.MediaRecorder && MediaRecorder.isTypeSupported(t)) { chosenMime = t; break; } } catch {}
    }

    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    const audioCtx = new AudioCtx();
    const source = audioCtx.createMediaStreamSource(stream);
    const analyser = audioCtx.createAnalyser();
    analyser.fftSize = 2048;
    source.connect(analyser);

    const session = {
      stream, chosenMime, audioCtx, analyser,
      vadData: new Float32Array(analyser.fftSize),
      noiseFloor: VAD_MIN_THRESHOLD,
      speechDetected: false, silenceStartAt: null,
      stopping: false, rafId: null,
      recorder: null,
      // Set by stopVoiceSession(session, true) -- distinct from voiceSession
      // being nulled out, since that can happen (a newer session replacing
      // this one) without this one actually being cancelled. Checked after
      // transcribe() resolves so a cancel clicked mid-transcription still
      // discards the result instead of inserting/sending it once STT
      // finishes in the background.
      cancelled: false,
    };
    session.recorder = createVoiceRecorder(session);
    voiceSession = session;
    // If Caroline is mid-reply out loud, pause it rather than talk over (or
    // get picked up by) the user starting to dictate -- resumes once this
    // recording ends (see processRecording/stopVoiceSession's discard path).
    if (activeSpeech?.audio && !activeSpeech.audio.paused) {
      try { activeSpeech.audio.pause(); } catch {}
    }
    session.recorder.start();
    setMicState("recording");
    updateStopBtnVisibility();
    session.rafId = requestAnimationFrame(() => runVadFrame(session));
  }

  function resumeSpeechIfPaused() {
    if (activeSpeech?.audio && activeSpeech.audio.paused && !activeSpeech.aborted) {
      activeSpeech.audio.play().catch(() => {});
    }
  }

  micBtn.addEventListener("click", toggleVoiceRecording);
  // Invoked from the WPF shell's Ctrl+Shift+C global hotkey (see
  // MainWindow.ToggleVoiceRecordingFromHotkey) -- works even while this
  // window is hidden to tray, since WebView2/this page keep running.
  window.carolineToggleVoiceRecording = toggleVoiceRecording;

  // Invoked from MainWindow's OnOpenLogin when the user clicks "I have my own
  // Claude account" on the noAiAtAll login window -- same ExecuteScriptAsync
  // mechanism as the hotkey above, just triggered from that window's callback
  // instead of a global hotkey.
  window.carolineOpenSettings = openSettings;

  // The WPF shell posts this once its floating viewer/editor window closes
  // (see DocumentViewerWindow.xaml.cs) -- forward it to the backend so the
  // open_in_viewer tool call it's blocked on can finally resolve.
  if (window.chrome?.webview) {
    window.chrome.webview.addEventListener("message", (e) => {
      const data = e.data;
      if (data && data.type === "editor_result") {
        sendControl("editor_result", { requestId: data.requestId, outcome: data.outcome, path: data.path, message: data.message });
      } else if (data && data.type === "login_result") {
        // The credentials never touch the model -- this goes straight to the
        // backend's own control channel, same shape as editor_result above.
        sendControl("login_submit", { requestId: data.requestId, cancelled: data.cancelled, email: data.email, password: data.password, isRegister: data.isRegister });
      } else if (data && data.type === "payment_result") {
        // No explicit success/fail signal needed here -- Revolut's webhook
        // confirms completion server-side; closing this window just means
        // "done browsing the checkout", so refresh the displayed balance.
        sendControl("sw_status");
      } else if (data && data.type === "update_status") {
        // The WPF shell's own self-updater downloading a new build -- purely
        // native, has nothing to do with any backend/query() turn. Per
        // explicit instruction (2026-09-06): this used to be completely
        // invisible for the whole multi-minute download; now shown here plus
        // a one-time tray popup (see MainWindow.BroadcastUpdateStatus).
        // Yellow, not red -- this isn't an error, just in progress.
        setStatus(data.text, "restarting");
      } else if (data && data.type === "visual_speech_done") {
        // VisualModeWindow finished playing (or failed to render/play) and has
        // already closed itself -- advance playOneSpeech's queue the same way
        // playAudioBase64 does on the <audio> element's "ended" event.
        const pending = pendingVisualDone.get(data.requestId);
        if (pending) {
          pendingVisualDone.delete(data.requestId);
          if (data.played === false) {
            // Visual playback didn't happen (model not warmed yet, or render/
            // playback failed natively) -- fall back to plain audio instead of
            // leaving this reply silent. Confirmed live (2026-09-03): right
            // after a restart the model can still be warming up when the
            // first voice reply comes in.
            playAudioBase64(pending.base64, pending.entry, pending.onDone);
          } else {
            if (activeSpeech === pending.entry) activeSpeech = null;
            pending.entry.btn?.classList.remove("speaking");
            pending.onDone?.();
          }
        }
      }
    });
  }

  function autoGrow() {
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 120) + "px";
  }

  sendBtn.addEventListener("click", () => send());
  // Voice input takes priority when both happen to be live at once (a turn
  // running while the user also starts dictating a follow-up) -- clicking
  // Stop then cancels the mic first; a second click after that would stop
  // the still-running turn, same as before this existed.
  stopBtn.addEventListener("click", () => {
    if (voiceSession) {
      stopVoiceSession(voiceSession, true);
      return;
    }
    stopCurrentTurn();
  });
  inputEl.addEventListener("input", autoGrow);
  inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });

  // Fires when the WPF shell actually closes the window (not on hide-to-tray,
  // which never unloads this page) -- the one natural point to ask the
  // backend for a best-effort final memory sync before it gets killed.
  window.addEventListener("pagehide", () => sendControl("shutdown_sync"));

  replayTranscript();
  connect();
  // Lamp 2 has no push channel (see updateChannelLamp's doc comment) --
  // polled at the channel's own cadence so the lamp never drifts far behind
  // its actual state. pollChannelStatus() itself no-ops via sendControl
  // whenever the WS happens to be down between reconnect attempts.
  setInterval(pollChannelStatus, 15000);
})();
