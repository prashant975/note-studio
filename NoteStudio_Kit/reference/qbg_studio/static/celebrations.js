/* QBG One - welcome, greetings and small celebrations.
 *
 * One queue, one thing on screen at a time, never over a running job or a
 * form being typed in. Toasts fade after a few seconds or on a click; a big
 * milestone adds a short confetti burst (a plain fade under
 * prefers-reduced-motion). All wording lives in messages.js; what is worth
 * celebrating is decided by the server (/celebrations/state) so the same
 * milestone never shows twice. Nothing loops when nothing is showing.
 *
 * Preview without doing real work, from the browser console:
 *   qbgCelebrate.test('batch_uploaded', {count: 45, seconds: 240})
 *   qbgCelebrate.test('milestone_hours', {hours: 10})
 *   qbgCelebrate.kinds()            // every kind you can preview
 *   qbgCelebrate.welcome()          // the first-launch welcome
 * Or open any page with ?celebrate-debug=1 for a small panel of buttons.
 */
(function () {
  "use strict";

  var LANG = "english";
  var MESSAGES = window.QBG_MESSAGES || { english: {}, hinglish: {} };
  var ENABLED = false;
  var REDUCED = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var ENC_KEY = "qbg-encourage";        // {at, last} in localStorage
  var ENC_GAP_MS = 2.5 * 60 * 60 * 1000; // at most one encouragement per ~2.5 h
  var queue = [];
  var showing = false;
  var host = null;

  function m(key) {
    var set = MESSAGES[LANG] || MESSAGES.english || {};
    if (key in set) return set[key];
    return (MESSAGES.english || {})[key];
  }

  function fill(text, tokens) {
    return String(text || "").replace(/\{(\w+)\}/g, function (whole, name) {
      return (tokens && tokens[name] != null) ? String(tokens[name]) : "";
    }).replace(/\s+/g, " ").trim();
  }

  function minutes(seconds) {
    if (!seconds || seconds < 60) return Math.max(1, Math.round(seconds || 0)) + " seconds";
    var mins = Math.round(seconds / 60);
    return mins === 1 ? "1 minute" : mins + " minutes";
  }

  function greetingKey(date) {
    var h = (date || new Date()).getHours();
    if (h >= 5 && h < 12) return "greeting_morning";
    if (h >= 12 && h < 17) return "greeting_afternoon";
    if (h >= 17 && h < 21) return "greeting_evening";
    return "greeting_night";
  }

  /* ---- is it a good moment? never interrupt real work ---- */
  function busy() {
    var panel = document.querySelector("[data-release-info]");
    // A running job shows the top-bar chip; also honour an explicit marker.
    if (document.querySelector(".topbar .chip.running, [data-job-running]")) return true;
    var el = document.activeElement;
    if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT"
               || el.isContentEditable)) return true;
    if (document.querySelector("dialog[open]")) return true;   // the welcome or update dialog
    return document.hidden;
  }

  /* ---- the queue: one at a time ---- */
  function enqueue(item) {
    if (!item) return;
    queue.push(item);
    pump();
  }

  function pump() {
    if (showing || !queue.length) return;
    if (busy()) { setTimeout(pump, 4000); return; }
    var item = queue.shift();
    showing = true;
    render(item, function () {
      showing = false;
      setTimeout(pump, 400);
    });
  }

  /* ---- rendering ---- */
  function ensureHost() {
    if (host) return host;
    host = document.getElementById("celebrations");
    if (!host) {
      host = document.createElement("div");
      host.id = "celebrations";
      host.setAttribute("aria-live", "polite");
      document.body.appendChild(host);
    }
    return host;
  }

  function render(item, done) {
    var big = item.size === "big";
    var card = document.createElement("div");
    card.className = "celebrate" + (big ? " big" : "");
    card.setAttribute("role", "status");
    var mark = document.createElement("div");
    mark.className = "celebrate-mark";
    mark.setAttribute("aria-hidden", "true");
    mark.innerHTML = big
      ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v4M12 18v4M2 12h4M18 12h4M5 5l2.5 2.5M16.5 16.5 19 19M19 5l-2.5 2.5M7.5 16.5 5 19"/></svg>'
      : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';
    var text = document.createElement("div");
    text.className = "celebrate-text";
    text.textContent = item.message;
    var close = document.createElement("button");
    close.type = "button";
    close.className = "celebrate-x";
    close.setAttribute("aria-label", m("close") || "Close");
    close.innerHTML = "&times;";
    card.appendChild(mark);
    card.appendChild(text);
    card.appendChild(close);
    ensureHost().appendChild(card);
    requestAnimationFrame(function () { card.classList.add("in"); });
    if (big && !REDUCED) confetti();

    var timer = setTimeout(dismiss, item.hold || (big ? 5000 : 3500));
    function dismiss() {
      clearTimeout(timer);
      card.classList.remove("in");
      card.classList.add("out");
      setTimeout(function () { card.remove(); done(); }, 260);
    }
    close.addEventListener("click", dismiss);
  }

  /* ---- a tiny confetti burst, canvas, no dependency (~1 KB) ---- */
  function confetti() {
    var canvas = document.createElement("canvas");
    canvas.className = "celebrate-confetti";
    var dpr = window.devicePixelRatio || 1;
    var w = canvas.width = Math.floor(window.innerWidth * dpr);
    var h = canvas.height = Math.floor(window.innerHeight * dpr);
    canvas.style.width = window.innerWidth + "px";
    canvas.style.height = window.innerHeight + "px";
    document.body.appendChild(canvas);
    var ctx = canvas.getContext("2d");
    var colors = ["#4f46e5", "#0f7b4f", "#9a5b00", "#b42318", "#2563eb", "#eab308"];
    var bits = [];
    for (var i = 0; i < 90; i++) {
      bits.push({
        x: w * (0.3 + 0.4 * Math.random()), y: h * 0.25,
        vx: (Math.random() - 0.5) * 9 * dpr, vy: (Math.random() * -9 - 4) * dpr,
        size: (4 + Math.random() * 4) * dpr, color: colors[i % colors.length],
        rot: Math.random() * 6.28, vr: (Math.random() - 0.5) * 0.4
      });
    }
    var start = null, gravity = 0.28 * dpr;
    function frame(t) {
      if (start === null) start = t;
      var elapsed = t - start;
      ctx.clearRect(0, 0, w, h);
      for (var i = 0; i < bits.length; i++) {
        var b = bits[i];
        b.vy += gravity; b.x += b.vx; b.y += b.vy; b.rot += b.vr;
        ctx.save(); ctx.translate(b.x, b.y); ctx.rotate(b.rot);
        ctx.fillStyle = b.color; ctx.globalAlpha = Math.max(0, 1 - elapsed / 1400);
        ctx.fillRect(-b.size / 2, -b.size / 2, b.size, b.size);
        ctx.restore();
      }
      if (elapsed < 1400) requestAnimationFrame(frame);
      else canvas.remove();
    }
    requestAnimationFrame(frame);
  }

  /* ---- turning a server event into a message ---- */
  function messageFor(kind, tokens) {
    tokens = tokens || {};
    if (kind === "batch_uploaded") {
      if (tokens.seconds) return fill(m("batch_uploaded"), { count: tokens.count, minutes: minutes(tokens.seconds) });
      return fill(m("batch_uploaded_plain"), tokens);
    }
    return fill(m(kind), tokens);
  }

  function celebrate(kind, tokens, size) {
    if (!ENABLED) return;
    var message = messageFor(kind, tokens);
    if (!message) return;
    enqueue({ kind: kind, message: message, tokens: tokens, size: size || "small" });
  }

  /* ---- welcome / welcome-back ---- */
  function welcomeModal() {
    if (typeof HTMLDialogElement !== "function") { welcomeBack(); return; }
    var dialog = document.createElement("dialog");
    dialog.className = "update-dialog";       // reuse the dialog styling
    var form = document.createElement("form");
    form.method = "dialog";
    var title = document.createElement("h2");
    title.textContent = m("welcome_title");
    var body = document.createElement("p");
    body.className = "update-meta";
    body.textContent = m("welcome_body");
    var actions = document.createElement("div");
    actions.className = "update-actions";
    var go = document.createElement("button");
    go.className = "btn sm primary";
    go.value = "go";
    go.textContent = m("welcome_button");
    actions.appendChild(go);
    form.appendChild(title); form.appendChild(body); form.appendChild(actions);
    dialog.appendChild(form);
    dialog.addEventListener("close", function () {
      dialog.remove();
      fetch("/celebrations/welcomed", { method: "POST" }).catch(function () {});
    });
    document.body.appendChild(dialog);
    dialog.showModal();
    go.focus();
  }

  function welcomeBack(name) {
    var key = name ? "welcome_back" : "welcome_back_noname";
    var text = fill(m(key), { greeting: m(greetingKey()), name: name || "" });
    enqueue({ kind: "welcome_back", message: text, size: "small", hold: 4500 });
  }

  /* ---- an occasional encouragement, never twice the same in a row ---- */
  function maybeEncourage() {
    if (!ENABLED) return;
    var list = (MESSAGES[LANG] || {}).encouragements || (MESSAGES.english || {}).encouragements || [];
    if (!list.length) return;
    var saved = {};
    try { saved = JSON.parse(localStorage.getItem(ENC_KEY) || "{}"); } catch (e) {}
    if (saved.at && Date.now() - saved.at < ENC_GAP_MS) return;
    var choices = list.filter(function (x) { return x !== saved.last; });
    var pick = choices[Math.floor(Math.random() * choices.length)] || list[0];
    enqueue({ kind: "encouragement", message: pick, size: "small" });
    try { localStorage.setItem(ENC_KEY, JSON.stringify({ at: Date.now(), last: pick })); } catch (e) {}
  }

  /* ---- talking to the server ---- */
  var seenWelcomeBack = false;
  async function poll(first) {
    if (document.hidden) return;
    try {
      var res = await fetch("/celebrations/state", { cache: "no-store" });
      var s = await res.json();
      ENABLED = !!s.enabled;
      LANG = s.language === "hinglish" ? "hinglish" : "english";
      if (!ENABLED) return;
      if (first) {
        if (!s.welcomed) welcomeModal();
        else if (!seenWelcomeBack) { seenWelcomeBack = true; welcomeBack(s.name); }
      }
      (s.events || []).forEach(function (ev) {
        enqueue({ kind: ev.kind, message: messageFor(ev.kind, ev.tokens), tokens: ev.tokens, size: ev.size });
      });
    } catch (e) { /* offline or starting; try again next tick */ }
  }

  /* ---- errors are never celebrated: a calm note instead ---- */
  function trouble(message) {
    if (window.QBG && window.QBG.toast) window.QBG.toast(message, "warn");
  }

  /* ---- debug panel ---- */
  function debugPanel() {
    var box = document.createElement("div");
    box.className = "celebrate-debug";
    var kinds = ["welcome", "welcome_back", "first_upload", "batch_uploaded", "translation_done",
                 "tagging_done", "milestone_uploaded", "milestone_hours", "milestone_streak", "encouragement"];
    kinds.forEach(function (k) {
      var b = document.createElement("button");
      b.type = "button"; b.className = "btn sm"; b.textContent = k;
      b.addEventListener("click", function () { api.test(k); });
      box.appendChild(b);
    });
    document.body.appendChild(box);
  }

  var SAMPLE = {
    first_upload: {}, batch_uploaded: { count: 45, seconds: 240 }, translation_done: {},
    tagging_done: {}, milestone_uploaded: { count: 1000 }, milestone_hours: { hours: 10 },
    milestone_streak: { days: 5 }, encouragement: {}
  };

  var api = {
    kinds: function () { return Object.keys(SAMPLE).concat(["welcome", "welcome_back"]); },
    welcome: function () { welcomeModal(); },
    test: function (kind, tokens) {
      var was = ENABLED; ENABLED = true;
      if (kind === "welcome") welcomeModal();
      else if (kind === "welcome_back") welcomeBack("Friend");
      else if (kind === "encouragement") {
        var list = (MESSAGES[LANG] || MESSAGES.english).encouragements || [];
        enqueue({ kind: "encouragement", message: list[0] || "Nice work.", size: "small" });
      } else {
        var big = kind === "first_upload" || kind === "milestone_uploaded" && (tokens || SAMPLE[kind]).count >= 1000
                  || kind === "milestone_hours" && (tokens || SAMPLE[kind]).hours >= 100;
        celebrate(kind, tokens || SAMPLE[kind], big ? "big" : "small");
      }
      ENABLED = was;
    },
    celebrate: celebrate,
    trouble: trouble,
    setLanguage: function (l) { LANG = l === "hinglish" ? "hinglish" : "english"; }
  };
  window.qbgCelebrate = api;

  function begin() {
    poll(true);
    setInterval(function () { poll(false); }, 45000);
    document.addEventListener("visibilitychange", function () { if (!document.hidden) poll(false); });
    // A light encouragement now and then, only when idle.
    setInterval(maybeEncourage, 20 * 60 * 1000);
    if (/[?&]celebrate-debug=1/.test(window.location.search)) debugPanel();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", begin);
  else begin();
})();
