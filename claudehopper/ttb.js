/**
 * TipTapBridge v4.1 — Event-driven bridge for claude.ai
 * 
 * Two pipes, zero polling:
 *   OUTBOUND: MutationObserver → <!CMD> regex → executor WebSocket
 *   INBOUND:  executor WebSocket → TipTap send → wait for response → next
 * 
 * Features:
 *   - Outbound queue (holds commands if executor disconnected)
 *   - Inbound queue (serialized, no stomping)
 *   - Pause/resume (Ctrl+Shift+P or tb.pause()/tb.resume())
 *   - TipTap editor API (no DOM hacks)
 *   - Works standalone without executor (manual tb.send/tb.queueMsg)
 * 
 * Executor API (window._remote):
 *   .emit(type, data)    — send to executor
 *   .addListener(type, fn) — receive from executor
 *   .addObserver/addInterval/addTimeout/addCleanup — lifecycle mgmt
 * 
 * DOM Map:
 *   .ProseMirror                          → TipTap editor (.editor)
 *   button[aria-label="Send message"]     → submit button
 *   [data-is-streaming="false"|"true"]    → response container
 *   .standard-markdown                    → response content
 *   .font-claude-response-body            → response paragraphs
 *   [data-testid="user-message"]          → user messages
 *   [data-testid="chat-input"]            → input container
 */

class TTB {

  constructor() {
    this._editor = null;
    this._observer = null;
    this._seen = new Set();
    this.outbox = [];
    this.inbox = [];
    this.paused = false;
    this._flushing = false;
  }

  // -- Editor --

  get editor() {
    if (!this._editor) {
      const pm = document.querySelector('.ProseMirror');
      if (pm && pm.editor) this._editor = pm.editor;
    }
    return this._editor;
  }

  // -- Input --

  setInput(text) {
    this.editor.commands.focus();
    this.editor.commands.setContent(text);
  }

  getInput() {
    return this.editor.getText();
  }

  clearInput() {
    this.editor.commands.clearContent();
  }

  send(text) {
    this.editor.commands.focus();
    this.editor.commands.setContent(text);
    setTimeout(() => {
      const btn = document.querySelector('button[aria-label="Send message"]');
      if (btn) btn.click();
    }, 100);
  }

  // -- Output --

  getLastResponse() {
    const msgs = document.querySelectorAll('[data-is-streaming="false"] .standard-markdown');
    if (!msgs.length) return null;
    return msgs[msgs.length - 1].textContent;
  }

  getStreamingResponse() {
    const msg = document.querySelector('[data-is-streaming="true"] .standard-markdown');
    return msg ? msg.textContent : null;
  }

  getLastResponseHTML() {
    const msgs = document.querySelectorAll('[data-is-streaming="false"] .standard-markdown');
    if (!msgs.length) return null;
    return msgs[msgs.length - 1].innerHTML;
  }

  getAllResponses() {
    const msgs = document.querySelectorAll('[data-is-streaming="false"] .standard-markdown');
    return Array.from(msgs).map(m => m.textContent);
  }

  getLastCodeBlocks() {
    const msgs = document.querySelectorAll('[data-is-streaming="false"] .standard-markdown');
    if (!msgs.length) return [];
    const last = msgs[msgs.length - 1];
    return Array.from(last.querySelectorAll('pre code')).map(c => c.textContent);
  }

  isStreaming() {
    return !!document.querySelector('[data-is-streaming="true"]');
  }

  // -- User messages --

  getLastUserMessage() {
    const msgs = document.querySelectorAll('[data-testid="user-message"]');
    if (!msgs.length) return null;
    return msgs[msgs.length - 1].textContent;
  }

  getUserMessageCount() {
    return document.querySelectorAll('[data-testid="user-message"]').length;
  }

  // -- Pause control (Ctrl+Shift+P) --

  pause() {
    this.paused = true;
    document.title = '\u23F8 PAUSED (' + this.inbox.length + ' queued)';
  }

  resume() {
    this.paused = false;
    document.title = '\u25B6 LIVE';
    this.flushInbox();
  }

  toggle() {
    this.paused ? this.resume() : this.pause();
  }

  // -- Outbound: <!CMD> from Claude → executor --

  queueCmd(json) {
    this.outbox.push(json);
    this.flushOutbox();
  }

  flushOutbox() {
    if (!window._remote?.emit) return;
    while (this.outbox.length) {
      window._remote.emit('cmd', this.outbox.shift());
    }
  }

  startOutbound() {
    if (this._observer) this._observer.disconnect();

    const self = this;
    this._observer = new MutationObserver(() => {
      const msgs = document.querySelectorAll('[data-is-streaming="false"] .standard-markdown');
      const last = msgs[msgs.length - 1];
      if (!last) return;
      const regex = /<!CMD\s*(.*?)>/g;
      let match;
      while ((match = regex.exec(last.textContent)) !== null) {
        if (self._seen.has(match[1])) continue;
        try {
          const json = JSON.parse(match[1]);
          self._seen.add(match[1]);
          self.queueCmd(json);
        } catch (e) {}
      }
    });

    this._observer.observe(document.body, { childList: true, subtree: true });
  }

  // -- Inbound: messages from executor → Claude (serialized) --

  queueMsg(text) {
    this.inbox.push(text);
    if (this.paused) {
      document.title = '\u23F8 PAUSED (' + this.inbox.length + ' queued)';
      return;
    }
    this.flushInbox();
  }

  flushInbox() {
    if (this.paused) return;
    if (this._flushing) return;
    if (this.isStreaming()) return;
    if (!this.inbox.length) return;

    this._flushing = true;
    this.send(this.inbox.shift());

    const self = this;
    const check = setInterval(() => {
      if (!self.isStreaming()) {
        clearInterval(check);
        self._flushing = false;
        self.flushInbox();
      }
    }, 500);
  }

  // -- Lifecycle --

  start() {
    // Outbound: watch responses for <!CMD>
    this.startOutbound();

    // Inbound: listen for executor messages (if available)
    if (window._remote?.addListener) {
      window._remote.addListener('msg', (text) => this.queueMsg(text));
      window._remote.addListener('reconnect', () => this.flushOutbox());
    }

    // Keyboard shortcut: Ctrl+Shift+P to toggle pause
    document.addEventListener('keydown', (e) => {
      if (e.ctrlKey && e.shiftKey && e.key === 'P') {
        e.preventDefault();
        this.toggle();
      }
    });

    // Start a drain loop — checks every 2s
    this._drainTimer = setInterval(() => {
      if (this.paused) return;
      if (this._flushing) return;
      if (this.isStreaming()) return;
      if (this.getInput().trim()) return;  // human typing
      if (!this.inbox.length) return;

      this._flushing = true;
      this.send(this.inbox.shift());
      setTimeout(() => { this._flushing = false; }, 3000);  // cooldown
    }, 2000);

    document.title = '\u25B6 LIVE';
  }

  stop() {
    if (this._observer) {
      this._observer.disconnect();
      this._observer = null;
    }
    if (this._drainTimer) {
      clearInterval(this._drainTimer);
      this._drainTimer = null;
    }
    this._seen.clear();
    this.outbox.length = 0;
    this.inbox.length = 0;
    this._flushing = false;
  }

  status() {
    return {
      paused: this.paused,
      outbox: this.outbox.length,
      inbox: this.inbox.length,
      seen: this._seen.size,
      streaming: this.isStreaming(),
      flushing: this._flushing,
      executor: !!window._remote?.emit
    };
  }
}