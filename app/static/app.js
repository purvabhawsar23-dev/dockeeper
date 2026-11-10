/* =========================================================
   app.js — DocKeeper frontend logic
   Plain vanilla JavaScript, no libraries or frameworks.
   All text from the server is inserted via textContent or
   createTextNode — NEVER innerHTML — to prevent XSS attacks.
   An attacker who uploads a document containing <script> tags
   cannot execute code if we never parse their text as HTML.
   ========================================================= */

"use strict";

/* ── Constants ───────────────────────────────────────────── */
const MAX_FILE_MB     = 200;
const MAX_IMAGE_MB    = 10;
const POLL_INTERVAL   = 2000; // ms between status polls
const ACTIVE_STATUSES = new Set(["uploaded", "processing"]);
const IMAGE_EXTS      = new Set(["png", "jpg", "jpeg", "webp"]);
const ALLOWED_EXTS    = new Set([
  "pdf", "docx", "xlsx", "txt", "md",
  "png", "jpg", "jpeg", "webp"
]);

/* ── DOM references ─────────────────────────────────────── */
const uploadBox    = document.getElementById("uploadBox");
const fileInput    = document.getElementById("fileInput");
const browseBtn    = document.getElementById("browseBtn");
const uploadStatus = document.getElementById("uploadStatus");
const docList      = document.getElementById("docList");
const chatMessages = document.getElementById("chatMessages");
const chatInput    = document.getElementById("chatInput");
const sendBtn      = document.getElementById("sendBtn");

/* ── Polling state ──────────────────────────────────────── */
// Map of doc_id -> intervalId for active polls.
// Stored here so we can cancel them when a document becomes ready/failed.
const activePolls = new Map();


/* ==========================================================
   UPLOAD
   ========================================================== */

browseBtn.addEventListener("click", () => fileInput.click());

fileInput.addEventListener("change", () => {
  handleFiles([...fileInput.files]);
  fileInput.value = ""; // reset so the same file can be re-selected
});

// Drag-and-drop events
uploadBox.addEventListener("dragover", (e) => {
  e.preventDefault();
  uploadBox.classList.add("drag-over");
});

uploadBox.addEventListener("dragleave", () => {
  uploadBox.classList.remove("drag-over");
});

uploadBox.addEventListener("drop", (e) => {
  e.preventDefault();
  uploadBox.classList.remove("drag-over");
  handleFiles([...e.dataTransfer.files]);
});

/** Upload an array of File objects one after another. */
async function handleFiles(files) {
  for (const file of files) {
    await uploadFile(file);
  }
}

/** Upload a single file; show inline status; refresh document list. */
async function uploadFile(file) {
  // Browser-side file type check
  const ext = file.name.split(".").pop().toLowerCase();
  if (!ALLOWED_EXTS.has(ext)) {
    showUploadMsg(
      `"${file.name}" is not supported. Allowed: PDF, DOCX, XLSX, TXT, MD, PNG, JPG, JPEG, WEBP.`,
      "error"
    );
    return;
  }

  // Browser-side size check — avoids sending an oversized file only to get a 413.
  // Images have a separate 10 MB limit to preserve Gemini OCR quota.
  const isImage = IMAGE_EXTS.has(ext);
  const maxMb   = isImage ? MAX_IMAGE_MB : MAX_FILE_MB;

  if (file.size > maxMb * 1024 * 1024) {
    showUploadMsg(
      `"${file.name}" is too large (${(file.size / 1048576).toFixed(1)} MB). Max ${maxMb} MB for ${isImage ? "images" : "documents"}.`,
      "error"
    );
    return;
  }

  const msg = showUploadMsg(`Uploading "${file.name}"…`, "");

  const form = new FormData();
  form.append("file", file);

  try {
    const res  = await fetch("/upload", { method: "POST", body: form });
    const data = await res.json();

    if (!res.ok) {
      setUploadMsg(msg, `"${file.name}": ${data.detail ?? res.statusText}`, "error");
      return;
    }

    setUploadMsg(msg, `"${file.name}" uploaded (id ${data.id}).`, "ok");
    await loadDocuments();         // refresh list immediately
    startPolling(data.id);        // watch processing progress

  } catch (err) {
    setUploadMsg(msg, `"${file.name}": network error — ${err.message}`, "error");
  }
}

/** Create and return an upload status message element. */
function showUploadMsg(text, kind) {
  const el = document.createElement("div");
  el.className = "upload-msg" + (kind ? " " + kind : "");
  el.textContent = text;   // textContent, not innerHTML — XSS safety
  uploadStatus.appendChild(el);
  // Auto-remove success messages after 6 seconds to keep the area clean
  if (kind === "ok") setTimeout(() => el.remove(), 6000);
  return el;
}

function setUploadMsg(el, text, kind) {
  el.textContent = text;
  el.className   = "upload-msg" + (kind ? " " + kind : "");
  if (kind === "ok") setTimeout(() => el.remove(), 6000);
}


/* ==========================================================
   DOCUMENT LIST
   ========================================================== */

/** Fetch /documents and re-render the list. */
async function loadDocuments() {
  try {
    const res  = await fetch("/documents");
    if (!res.ok) { showDocError("Failed to load documents: " + res.statusText); return; }
    const docs = await res.json();
    renderDocList(docs);
  } catch (err) {
    showDocError("Network error loading documents: " + err.message);
  }
}

function showDocError(msg) {
  docList.textContent = "";
  const p = document.createElement("p");
  p.className  = "empty-hint";
  p.textContent = msg;   // textContent — safe
  docList.appendChild(p);
}

/** Build the full document list from an array of doc objects. */
function renderDocList(docs) {
  docList.textContent = "";  // clear safely (removes all children)

  if (docs.length === 0) {
    const p = document.createElement("p");
    p.className  = "empty-hint";
    p.textContent = "No documents yet.";
    docList.appendChild(p);
    return;
  }

  for (const doc of docs) {
    docList.appendChild(buildDocCard(doc));
    // Resume polling for any document still being processed after a page reload
    if (ACTIVE_STATUSES.has(doc.status) && !activePolls.has(doc.id)) {
      startPolling(doc.id);
    }
  }
}

/** Create a document card DOM element. */
function buildDocCard(doc) {
  const card = document.createElement("div");
  card.className   = "doc-card";
  card.dataset.id  = doc.id;

  // Name
  const name = document.createElement("div");
  name.className  = "doc-name";
  name.textContent = doc.filename;    // textContent — XSS safe
  card.appendChild(name);

  // Meta row: type · pages · badge
  const meta = document.createElement("div");
  meta.className = "doc-meta";

  const type = document.createElement("span");
  type.textContent = doc.file_type?.toUpperCase() ?? "?";
  meta.appendChild(type);

  if (doc.page_count != null) {
    const pages = document.createElement("span");
    pages.textContent = `${doc.page_count} page${doc.page_count !== 1 ? "s" : ""}`;
    meta.appendChild(pages);
  }

  const badge = document.createElement("span");
  badge.className  = `badge badge-${doc.status}`;
  badge.textContent = doc.status;
  meta.appendChild(badge);

  card.appendChild(meta);

  // Progress line (shown while processing)
  if (ACTIVE_STATUSES.has(doc.status)) {
    const prog = document.createElement("div");
    prog.className   = "doc-progress";
    prog.dataset.id  = doc.id;
    prog.textContent = "Waiting for progress…";
    card.appendChild(prog);
  }

  // Error message + reprocess button for failed docs
  if (doc.status === "failed") {
    // Error text is loaded from the status endpoint in updateDocCard()
    const errEl = document.createElement("div");
    errEl.className  = "doc-error";
    errEl.dataset.errId = doc.id;
    errEl.textContent   = "Loading error details…";
    card.appendChild(errEl);
    fetchAndShowError(doc.id, errEl);  // fill in the error message asynchronously

    const reprocessBtn = document.createElement("button");
    reprocessBtn.className  = "btn btn-secondary btn-sm";
    reprocessBtn.textContent = "⟳ Reprocess";
    reprocessBtn.addEventListener("click", () => reprocessDocument(doc.id));

    const actions = document.createElement("div");
    actions.className = "doc-actions";
    actions.appendChild(reprocessBtn);
    card.appendChild(actions);
  }

  // Delete button — always visible
  const deleteBtn = document.createElement("button");
  deleteBtn.className  = "btn btn-danger btn-sm";
  deleteBtn.textContent = "🗑 Delete";
  deleteBtn.addEventListener("click", () => deleteDocument(doc.id, doc.filename));

  const actions = card.querySelector(".doc-actions") ?? document.createElement("div");
  if (!actions.className) actions.className = "doc-actions";
  actions.appendChild(deleteBtn);
  if (!card.contains(actions)) card.appendChild(actions);

  return card;
}

/** Fetch the status endpoint to fill in the error_message for a failed doc. */
async function fetchAndShowError(id, errEl) {
  try {
    const res  = await fetch(`/documents/${id}/status`);
    const data = await res.json();
    // textContent — the error message comes from the server but we never eval it
    errEl.textContent = data.error_message
      ? `Error: ${data.error_message}`
      : "Processing failed (no details available).";
  } catch {
    errEl.textContent = "Could not load error details.";
  }
}

/** Update just the progress line in an existing card. */
function updateProgressLine(id, text) {
  const el = docList.querySelector(`.doc-progress[data-id="${id}"]`);
  if (el) el.textContent = text;   // textContent — safe
}


/* ==========================================================
   POLLING
   ========================================================== */

/** Start polling the status endpoint for doc `id` every 2 seconds. */
function startPolling(id) {
  if (activePolls.has(id)) return;  // already polling

  const intervalId = setInterval(async () => {
    try {
      const res  = await fetch(`/documents/${id}/status`);
      const data = await res.json();

      updateProgressLine(id, `${data.chunks_stored ?? 0} chunks stored`);

      // Stop polling once the document is no longer active
      if (!ACTIVE_STATUSES.has(data.status)) {
        clearInterval(intervalId);
        activePolls.delete(id);
        // Refresh the full list so the badge, page count, etc. update
        await loadDocuments();
      }
    } catch {
      // Network hiccup — keep polling; don't stop on a transient error
    }
  }, POLL_INTERVAL);

  activePolls.set(id, intervalId);
}


/* ==========================================================
   DOCUMENT ACTIONS
   ========================================================== */

async function deleteDocument(id, filename) {
  // Confirm before deleting — data is unrecoverable
  if (!confirm(`Delete "${filename}"? This cannot be undone.`)) return;

  try {
    const res = await fetch(`/documents/${id}`, { method: "DELETE" });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      alert(`Delete failed: ${data.detail ?? res.statusText}`);
      return;
    }
    // Stop polling if running for this document
    if (activePolls.has(id)) {
      clearInterval(activePolls.get(id));
      activePolls.delete(id);
    }
    await loadDocuments();
  } catch (err) {
    alert("Network error while deleting: " + err.message);
  }
}

async function reprocessDocument(id) {
  try {
    const res = await fetch(`/documents/${id}/process`, { method: "POST" });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      alert(`Reprocess failed: ${data.detail ?? res.statusText}`);
      return;
    }
    await loadDocuments();
    startPolling(id);
  } catch (err) {
    alert("Network error while reprocessing: " + err.message);
  }
}


/* ==========================================================
   CHAT
   ========================================================== */

// Enter sends; Shift+Enter inserts a new line (standard chat UX)
chatInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendQuestion();
  }
});

sendBtn.addEventListener("click", sendQuestion);

async function sendQuestion() {
  const question = chatInput.value.trim();
  if (!question) return;

  chatInput.value = "";
  setSendDisabled(true);

  // Create a turn container that holds the question bubble + answer
  const turn = document.createElement("div");
  turn.className = "chat-turn";

  // Question bubble
  const qBubble = document.createElement("div");
  qBubble.className   = "bubble-question";
  qBubble.textContent = question;    // textContent — never innerHTML
  turn.appendChild(qBubble);

  // Thinking placeholder
  const aBubble = document.createElement("div");
  aBubble.className   = "bubble-answer bubble-thinking";
  aBubble.textContent = "Thinking…";
  turn.appendChild(aBubble);

  chatMessages.appendChild(turn);
  scrollToBottom();

  try {
    const res  = await fetch("/chat", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ question }),
    });
    const data = await res.json();

    if (!res.ok) {
      aBubble.className   = "bubble-answer";
      aBubble.textContent = `Error: ${data.detail ?? res.statusText}`;
      return;
    }

    // Replace "Thinking…" with the real answer
    aBubble.className   = "bubble-answer";
    renderAnswer(aBubble, data.answer, data.sources ?? [], turn);

  } catch (err) {
    aBubble.className   = "bubble-answer";
    aBubble.textContent = "Network error: " + err.message;
  } finally {
    setSendDisabled(false);
    scrollToBottom();
  }
}

function setSendDisabled(disabled) {
  sendBtn.disabled    = disabled;
  chatInput.disabled  = disabled;
}

function scrollToBottom() {
  chatMessages.scrollTop = chatMessages.scrollHeight;
}

/**
 * Parse inline text into DOM elements, handling:
 * - **bold** (<strong>)
 * - *italic* (<em>)
 * - [n] and [n, m] citation badges (<span class="cite-badge">)
 * - Plain text nodes for everything else
 *
 * Uses ONLY document.createElement, createTextNode, and textContent.
 * NEVER innerHTML, outerHTML, or insertAdjacentHTML.
 */
function renderInline(parent, text, container) {
  if (!text) return;

  // Regex matches:
  // 1 & 2: **bold**
  // 3 & 4: *italic*
  // 5 & 6: [citations] like [1] or [2, 3] or [4,5]
  const inlineRe = /(\*\*([^\s*](?:[\s\S]*?[^\s*])?)\*\*)|(\*([^\s*](?:[^*]*?[^\s*])?)\*)|(\[(\d[\d,\s]*)\])/g;

  let lastIndex = 0;
  let match;

  while ((match = inlineRe.exec(text)) !== null) {
    // Append any plain text before this match as a pure text node
    if (match.index > lastIndex) {
      parent.appendChild(
        document.createTextNode(text.slice(lastIndex, match.index))
      );
    }

    if (match[1] !== undefined) {
      // Bold: match[2] is the text inside **...**
      const strong = document.createElement("strong");
      renderInline(strong, match[2], container);
      parent.appendChild(strong);
    } else if (match[3] !== undefined) {
      // Italic: match[4] is the text inside *...*
      const em = document.createElement("em");
      renderInline(em, match[4], container);
      parent.appendChild(em);
    } else if (match[5] !== undefined) {
      // Citation: match[6] is the string of numbers/commas
      const nums = match[6]
        .split(",")
        .map((s) => parseInt(s.trim(), 10))
        .filter(Number.isFinite);

      for (const n of nums) {
        const badge = document.createElement("span");
        badge.className = "cite-badge";
        badge.textContent = `[${n}]`; // textContent — safe from XSS

        // Clicking a badge scrolls to and highlights the matching source card
        badge.addEventListener("click", () => {
          const turn =
            badge.closest(".chat-turn") ||
            (container ? container.closest(".chat-turn") : null);
          if (turn) highlightSource(turn, n);
        });
        parent.appendChild(badge);
      }
    }

    lastIndex = match.index + match[0].length;
  }

  // Append any trailing text after the last match
  if (lastIndex < text.length) {
    parent.appendChild(document.createTextNode(text.slice(lastIndex)));
  }
}

/**
 * Render structured markdown-like text (paragraphs, bullet lists, ordered lists,
 * bold, italic, and citation badges) into a container element.
 *
 * WHY BUILD ELEMENTS INSTEAD OF USING innerHTML (XSS Prevention):
 * Documents uploaded by users or third parties could contain malicious HTML
 * (such as <script>, <img onerror=...>, <iframe src=...>). If we parsed the
 * text with innerHTML, an attacker could execute arbitrary JavaScript in the
 * user's browser session. By building nodes programmatically and setting text
 * exclusively through textContent/createTextNode, the browser strictly treats
 * all input as text/data, preventing script injection.
 */
function renderRichText(container, text) {
  container.textContent = ""; // Clear existing content (e.g. "Thinking…")
  if (!text) return;

  const lines = text.split(/\r?\n/);
  let currentParagraph = null;
  let currentList = null; // { type: 'ul' | 'ol', el: HTMLElement }

  function closeParagraph() {
    currentParagraph = null;
  }

  function closeList() {
    currentList = null;
  }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const trimmed = line.trim();

    if (!trimmed) {
      // Blank line: ends open paragraph and list
      closeParagraph();
      closeList();
      continue;
    }

    // Bullet list item: "* " or "- "
    const bulletMatch = line.match(/^\s*[\*\-]\s+(.*)$/);
    if (bulletMatch) {
      closeParagraph();
      if (!currentList || currentList.type !== "ul") {
        closeList();
        const ul = document.createElement("ul");
        container.appendChild(ul);
        currentList = { type: "ul", el: ul };
      }
      const li = document.createElement("li");
      renderInline(li, bulletMatch[1], container);
      currentList.el.appendChild(li);
      continue;
    }

    // Ordered list item: "1. ", "2. ", etc.
    const orderedMatch = line.match(/^\s*(\d+)\.\s+(.*)$/);
    if (orderedMatch) {
      closeParagraph();
      if (!currentList || currentList.type !== "ol") {
        closeList();
        const ol = document.createElement("ol");
        container.appendChild(ol);
        currentList = { type: "ol", el: ol };
      }
      const li = document.createElement("li");
      renderInline(li, orderedMatch[2], container);
      currentList.el.appendChild(li);
      continue;
    }

    // Regular line of text
    closeList();
    if (!currentParagraph) {
      currentParagraph = document.createElement("p");
      container.appendChild(currentParagraph);
    } else {
      // Preserve single line break inside the same paragraph
      currentParagraph.appendChild(document.createElement("br"));
    }
    renderInline(currentParagraph, line, container);
  }
}

/**
 * Render an answer with rich text formatting (markdown paragraphs, lists,
 * bold, italic, and clickable [n] citation badges), then append source cards.
 */
function renderAnswer(container, answerText, sources, turn) {
  renderRichText(container, answerText);

  // Append source cards if any were returned
  if (sources.length > 0) {
    const block = buildSourcesBlock(sources);
    turn.appendChild(block);
  }
}

/** Scroll to and briefly highlight the source card with number n. */
function highlightSource(turn, n) {
  const card = turn.querySelector(`.source-card[data-n="${n}"]`);
  if (!card) return;

  card.scrollIntoView({ behavior: "smooth", block: "nearest" });

  card.classList.remove("highlight");
  // Force a reflow so removing then re-adding the class triggers the transition
  void card.offsetWidth;
  card.classList.add("highlight");
  setTimeout(() => card.classList.remove("highlight"), 1800);
}

/** Build the block of source cards for one answer. */
function buildSourcesBlock(sources) {
  const block = document.createElement("div");
  block.className = "sources-block";

  for (const src of sources) {
    const card = document.createElement("div");
    card.className     = "source-card";
    card.dataset.n     = src.n;

    // Clickable header: [n] filename  page N  ▾
    const header = document.createElement("div");
    header.className   = "source-header";

    const nEl = document.createElement("span");
    nEl.className   = "source-n";
    nEl.textContent  = `[${src.n}]`;    // textContent — safe

    const nameEl = document.createElement("span");
    nameEl.className   = "source-name";
    nameEl.textContent  = src.filename;  // textContent — XSS safety: filename from server

    const pageEl = document.createElement("span");
    pageEl.className   = "source-page";
    pageEl.textContent  = `page ${src.page_number}`;

    const caret = document.createElement("span");
    caret.className   = "source-caret";
    caret.textContent  = "▾";
    caret.setAttribute("aria-hidden", "true");

    header.appendChild(nEl);
    header.appendChild(nameEl);
    header.appendChild(pageEl);
    header.appendChild(caret);

    // Collapsible body: full chunk content
    const body = document.createElement("div");
    body.className   = "source-body";
    body.textContent  = src.content ?? src.preview ?? "";  // textContent — safe

    // Toggle expand/collapse on header click
    header.addEventListener("click", () => {
      const open = body.classList.toggle("open");
      caret.textContent = open ? "▴" : "▾";
    });

    card.appendChild(header);
    card.appendChild(body);
    block.appendChild(card);
  }

  return block;
}


/* ==========================================================
   INIT
   ========================================================== */

// Load the document list as soon as the page is ready
loadDocuments();
