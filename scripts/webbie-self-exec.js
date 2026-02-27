// Webbie Self-Exec — drop into browser console on claude.ai
// Watches for { "js_cmd": "..." } in assistant messages, executes directly.
// Results are typed back into chat so Webbie sees them in context.
//
// Usage: paste into JS console, or inject via remote.js

(function() {
  'use strict';
  if (window._selfExec) { console.log('[self-exec] Already running'); return; }
  window._selfExec = { processed: new Set(), results: {} };

  const CMD_RE = /\{\s*"js_cmd"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}/g;

  // Get page nonce for CSP-safe script execution
  function getNonce() {
    const s = document.querySelector('script[nonce]');
    return s ? s.nonce || s.getAttribute('nonce') : '';
  }

  // Execute JS via nonce'd script element (bypasses CSP eval restriction)
  function execJS(code) {
    return new Promise((resolve) => {
      const id = '_se_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6);
      // Wrap in async IIFE so awaits work, capture result on window
      const wrapped = `
        (async () => {
          try {
            const __result = await (async function() { ${code} })();
            window._selfExec.results['${id}'] = { ok: true, value: __result };
          } catch(e) {
            window._selfExec.results['${id}'] = { ok: false, error: e.message };
          }
        })();
      `;
      const el = document.createElement('script');
      el.nonce = getNonce();
      el.textContent = wrapped;
      document.head.appendChild(el);
      el.remove();

      // Poll for result (script runs sync or near-sync)
      let tries = 0;
      const poll = setInterval(() => {
        const r = window._selfExec.results[id];
        if (r || tries++ > 100) {
          clearInterval(poll);
          delete window._selfExec.results[id];
          resolve(r || { ok: false, error: 'timeout' });
        }
      }, 50);
    });
  }

  // Type text into ProseMirror and send it
  function sendToChat(text) {
    const editor = document.querySelector('div.ProseMirror');
    if (!editor) { console.warn('[self-exec] No ProseMirror editor'); return false; }

    editor.focus();
    document.execCommand('selectAll');
    document.execCommand('delete');
    document.execCommand('insertText', false, text);

    // Click send button after a brief delay for ProseMirror to process
    setTimeout(() => {
      const btn = document.querySelector('button[aria-label="Send message"]')
                || document.querySelector('button[aria-label="Send Message"]');
      if (btn && !btn.disabled) {
        btn.click();
      } else {
        // Fallback: Enter key
        editor.dispatchEvent(new KeyboardEvent('keydown', {
          key: 'Enter', code: 'Enter', keyCode: 13, which: 13,
          bubbles: true, cancelable: true
        }));
      }
    }, 200);
    return true;
  }

  // Get text content of the last assistant message
  function getLastAssistantText() {
    // Try streaming-aware selectors first
    const msgs = document.querySelectorAll('[data-is-streaming]');
    const containers = msgs.length > 0
      ? Array.from(msgs)
      : Array.from(document.querySelectorAll('div.grid.grid-cols-1'));

    // Walk backwards to find last assistant message
    for (let i = containers.length - 1; i >= 0; i--) {
      const el = containers[i];
      const isUser = el.className.includes('font-user-message')
                  || el.className.includes('!font-user');
      const isStreaming = el.getAttribute('data-is-streaming') === 'true';
      if (!isUser && !isStreaming) {
        return el.textContent.trim();
      }
    }
    return '';
  }

  // Scan for new js_cmd blocks and execute them
  async function scan() {
    const text = getLastAssistantText();
    if (!text) return;

    let match;
    CMD_RE.lastIndex = 0;
    while ((match = CMD_RE.exec(text)) !== null) {
      const raw = match[0];
      const hash = raw.length + '_' + raw.slice(0, 40);
      if (window._selfExec.processed.has(hash)) continue;
      window._selfExec.processed.add(hash);

      // Unescape the JSON string value
      const code = match[1].replace(/\\"/g, '"').replace(/\\n/g, '\n').replace(/\\\\/g, '\\');
      console.log('[self-exec] Executing:', code.slice(0, 80));

      const result = await execJS(code);
      const output = result.ok
        ? String(result.value === undefined ? '(undefined)' : result.value)
        : `ERROR: ${result.error}`;

      console.log('[self-exec] Result:', output.slice(0, 200));

      // Feed result back into chat so Webbie sees it
      const reply = `[js_cmd result] ${output.slice(0, 3000)}`;
      sendToChat(reply);

      // Only execute one command per scan to avoid flooding
      break;
    }
  }

  // Watch for DOM changes with MutationObserver
  const observer = new MutationObserver(() => {
    // Debounce — don't scan while streaming
    clearTimeout(window._selfExec._debounce);
    window._selfExec._debounce = setTimeout(scan, 2000);
  });

  // Observe the main chat container
  const chatContainer = document.querySelector('main') || document.body;
  observer.observe(chatContainer, { childList: true, subtree: true, characterData: true });

  // Also poll every 10s as fallback (MutationObserver can miss some updates)
  setInterval(scan, 10000);

  console.log('[self-exec] Webbie self-exec active. Watching for { "js_cmd": "..." } in assistant messages.');
  console.log('[self-exec] Results will be typed back into chat automatically.');
})();
