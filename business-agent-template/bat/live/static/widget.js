/* The website chat widget.
 *
 * Served by the app at /chat/widget.js rather than copied into the marketing page, so the
 * chat can be fixed without touching the site — and so it lives in git. It was being
 * edited in place on the server with scp, which meant the version customers were running
 * existed nowhere else and no change to it was reviewable.
 *
 * The page supplies the markup and the styling; this only needs three ids:
 *   #chat-log     where messages go
 *   #chat-input   the one input box, used first for the number and then for messages
 *   #chat-send    the button
 */
/* Chat widget — wired to the AI receptionist on the apex domain.
     POST {API}/chat/new     { phone }             -> { session_id, greeting, call_us }
     POST {API}/chat/message { session_id, text }  -> 202 { status: 'working' }
     POST {API}/chat/poll    { session_id }        -> { status: working|ready|error|idle }

   The reply does not come back on the request that sent the message. The first turn was
   measured at 129 seconds — the assistant looks up the customer, reads the rules, checks
   the diary and prices the call-out — and Cloudflare cuts an idle connection at 100. The
   answer was written, stored, and never seen: this fetch rejected and the panel said we
   were offline while the assistant was still working.
   Cross-origin on purpose: the app allows this origin (web_chat_origin_list).

   The number is asked for first, in the same input box the messages use — no extra
   field, so the layout is untouched. Taking it up front means the assistant never has
   to interrupt somebody mid-way through describing a leak to ask for it, and nothing
   is spent on a reply until there is a number to attach the job to.

   The session id is kept in sessionStorage so a reload continues the thread rather than
   starting a stranger's conversation over. The number lives on the server against that
   id, so it is never re-asked and never travels with each message. */
(function () {
  /* Wherever this script was loaded from — never a hardcoded host.
   *
   * It was hardcoded, and that is a second source of truth for the one setting that has
   * to match the server's CORS allow-list exactly. A widget served from a staging box
   * and pointed at production is a page that looks like it is being tested and is not.
   *
   * `document.currentScript` is the tag being executed, which is this one, because this
   * runs at parse time rather than from a handler. */
  var here = document.currentScript && document.currentScript.src;
  var API = here ? here.replace(/\/chat\/widget\.js.*$/, '') : window.location.origin;
  var KEY = 'bat_chat_session';

  var log = document.getElementById('chat-log');
  var input = document.getElementById('chat-input');
  var send = document.getElementById('chat-send');

  var sessionId = null;
  var callUs = '(778) 200-4962';   /* replaced by call_us from the server */
  var awaitingPhone = false;
  var busy = false;

  function bubble(who, text) {
    var el = document.createElement('div');
    var mine = who === 'me';
    el.style.cssText =
      'align-self:' + (mine ? 'flex-end' : 'flex-start') + ';' +
      'max-width:78%;' +
      'background:' + (mine ? 'var(--color-accent)' : 'var(--color-surface)') + ';' +
      'color:' + (mine ? '#fff' : 'var(--color-text)') + ';' +
      'padding:12px 16px;border-radius:18px;font-size:15px;line-height:1.5;' +
      'white-space:pre-wrap;box-shadow:var(--shadow-sm)';
    el.textContent = text;
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
    return el;
  }

  /* Replies take a few seconds — without this the panel looks dead. */
  function typing(on, doing) {
    var existing = document.getElementById('chat-typing');
    if (!on) {
      if (existing) existing.remove();
      return;
    }
    if (existing) {
      /* The server says what it is working on. A minute of three dots looks the same as
         a system that has died, and the customer cannot tell which they are looking at. */
      if (doing) existing.textContent = doing + ' · · ·';
      return;
    }
    var el = bubble('bot', doing ? doing + ' · · ·' : '· · ·');
    el.id = 'chat-typing';
    el.style.opacity = '.65';
  }

  function setBusy(on) {
    busy = on;
    send.disabled = on;
    input.disabled = on;
    send.style.opacity = on ? '.55' : '';
    send.style.cursor = on ? 'default' : '';
  }

  function failureText(status) {
    if (status === 429) return "You're sending those faster than I can answer — give it a moment, or call " + callUs + '.';
    if (status === 413) return 'That message is a little long for me — could you split it up?';
    return "Sorry, I couldn't reach our assistant just now. Please call " + callUs + " and we'll pick up.";
  }

  /* The one input box does both jobs. Switching it is a placeholder and a button label:
     no element is added or removed, so nothing in the layout can shift. */
  function askForPhone() {
    awaitingPhone = true;
    input.placeholder = 'e.g. 604 555 0123';
    input.setAttribute('inputmode', 'tel');
    input.setAttribute('autocomplete', 'tel');
    send.textContent = 'Start';
  }

  function readyToChat() {
    awaitingPhone = false;
    input.placeholder = 'e.g. no hot water since this morning';
    input.setAttribute('inputmode', 'text');
    input.setAttribute('autocomplete', 'off');
    send.textContent = 'Send';
  }

  /* Backend unreachable. Say so rather than pretending to listen. */
  function offline() {
    typing(false);
    bubble('bot', 'Sorry — our chat assistant is offline right now. Please call ' + callUs + " and we'll pick up.");
    send.disabled = true;
    input.disabled = true;
    busy = true;
    input.placeholder = 'Chat unavailable — please call ' + callUs;
  }

  /* Ask every second and a half until there is an answer. The typing dots stay up the
     whole time, which is the truth: somebody is working on it. The ceiling is generous
     because the honest alternative — giving up while the assistant is still going — is
     the exact failure this replaced. */
  var POLL_EVERY_MS = 1500;
  var GIVE_UP_AFTER_MS = 5 * 60 * 1000;

  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  async function waitForReply() {
    var deadline = Date.now() + GIVE_UP_AFTER_MS;
    while (Date.now() < deadline) {
      await sleep(POLL_EVERY_MS);
      var response = await fetch(API + '/chat/poll', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId })
      });
      if (!response.ok) continue;          /* a blip; the answer is still being written */
      var data = await response.json();
      if (data.status === 'working') typing(true, data.doing);
      if (data.status === 'ready') return data.reply || failureText(0);
      if (data.status === 'error') return data.message || failureText(0);
      if (data.status === 'idle') return failureText(0);   /* restarted mid-turn */
    }
    return "That's taken longer than it should. Please call " + callUs + " and we'll pick up.";
  }

  async function startSession(typed) {
    setBusy(true);
    try {
      var response = await fetch(API + '/chat/new', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ phone: typed })
      });
      var data = await response.json();
      if (!response.ok) {
        /* Wrong-looking number: the server says what it wants, so repeat it verbatim
           rather than inventing a second wording of the same rule. */
        bubble('bot', data.message || "That doesn't look like a number we can reach you on — could you check it?");
        setBusy(false);
        return;
      }
      sessionId = data.session_id;
      if (data.call_us) callUs = data.call_us;
      try { sessionStorage.setItem(KEY, sessionId); } catch (e) { /* private mode */ }
      readyToChat();
      bubble('bot', data.greeting || "Thanks — what can we help you with?");
      setBusy(false);
    } catch (e) {
      offline();
    }
  }

  async function submit() {
    var text = input.value.trim();
    if (!text || busy) return;
    input.value = '';
    bubble('me', text);

    if (awaitingPhone) {
      await startSession(text);
      input.focus();
      return;
    }

    setBusy(true);
    typing(true);

    try {
      var response = await fetch(API + '/chat/message', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId, text: text })
      });
      if (response.status === 202) {
        var answer = await waitForReply();
        typing(false);
        bubble('bot', answer);
        return;
      }
      typing(false);
      if (response.status === 404) {
        /* The server has never heard of this session — the database was reset, or the id
           came from a much older visit. Ask again rather than leaving a box that swallows
           everything typed into it. */
        sessionId = null;
        try { sessionStorage.removeItem(KEY); } catch (e) { /* private mode */ }
        askForPhone();
        bubble('bot', "This chat has expired. What number can we reach you on?");
      } else if (!response.ok) {
        bubble('bot', failureText(response.status));
      } else {
        var data = await response.json();
        bubble('bot', data.reply || failureText(0));
      }
    } catch (e) {
      typing(false);
      bubble('bot', failureText(0));
    } finally {
      setBusy(false);
      input.focus();
    }
  }

  send.addEventListener('click', submit);
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  });

  /* No request on page load. Opening the panel is free, and a visitor who never types
     anything costs nothing — which also means an unreachable backend is discovered on
     the first thing they send rather than on every page view. */
  (function init() {
    try { sessionId = sessionStorage.getItem(KEY); } catch (e) { /* private mode */ }

    if (sessionId) {
      readyToChat();
      bubble('bot', 'Welcome back — carry on where you left off.');
      return;
    }

    askForPhone();
    bubble('bot', "Hi — Fangxin Plumbing. What number can we reach you on? We use it to look up your service history, appointments and warranty records.");
  })();
})();
