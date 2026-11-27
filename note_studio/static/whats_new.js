/* NoteCraft - the "What's new" tour (QBG One's whats_new.js, adapted).
 * Shown on the first page after a start, at most twice per release; "Got it"
 * ends it; "What's new" in the account menu opens it again any time. */
(function () {
  if (document.querySelector('[data-auth-start]')) return; // Show sign-in before the release tour.
  "use strict";
  var dialog = null, tour = null, index = 0;

  function el(tag, cls, words) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (words) node.textContent = words;
    return node;
  }

  function send(done) {
    if (!tour) return;
    var body = new URLSearchParams();
    body.set("tour", tour.id);
    body.set("done", done ? "1" : "");
    try { fetch("/whats-new/seen", { method: "POST", body: body, keepalive: true }); } catch (e) { /* never block */ }
  }

  function close(done) {
    send(done);
    if (dialog) { dialog.close(); dialog.remove(); }
    dialog = null;
  }

  function render() {
    var step = tour.steps[index];
    var body = dialog.querySelector("[data-tour-body]");
    body.textContent = "";
    body.appendChild(el("p", "tour-count", "New · " + (index + 1) + " of " + tour.steps.length));
    body.appendChild(el("h2", "", step.title));
    body.appendChild(el("p", "", step.text));
    if (step.href) {
      var show = el("a", "btn sm", "Show me");
      show.href = step.href;
      show.addEventListener("click", function () { close(index === tour.steps.length - 1); });
      body.appendChild(show);
    }
    var dots = el("div", "tour-dots");
    tour.steps.forEach(function (_s, i) { dots.appendChild(el("span", "tour-dot" + (i === index ? " on" : ""))); });
    body.appendChild(dots);
    dialog.querySelector("[data-tour-back]").hidden = index === 0;
    dialog.querySelector("[data-tour-next]").textContent = index === tour.steps.length - 1 ? "Got it" : "Next";
  }

  function open(value) {
    if (!value || !value.steps || !value.steps.length || dialog) return;
    if (document.querySelector("dialog[open]")) return;
    tour = value; index = 0;
    dialog = el("dialog", "card whats-new");
    var head = el("div", "hd");
    head.appendChild(el("h2", "grow", value.title || "What's new"));
    var body = el("div", "bd"); body.setAttribute("data-tour-body", "");
    var foot = el("div", "ft btn-row");
    var later = el("button", "btn ghost", value.last ? "Close" : "Show me next time");
    later.type = "button";
    later.addEventListener("click", function () { close(!!value.last); });
    var back = el("button", "btn", "Back"); back.type = "button"; back.setAttribute("data-tour-back", "");
    back.addEventListener("click", function () { index = Math.max(0, index - 1); render(); });
    var next = el("button", "btn primary", "Next"); next.type = "button"; next.setAttribute("data-tour-next", "");
    next.addEventListener("click", function () {
      if (index >= tour.steps.length - 1) { close(true); return; }
      index += 1; render();
    });
    foot.appendChild(later); foot.appendChild(back); foot.appendChild(next);
    dialog.appendChild(head); dialog.appendChild(body); dialog.appendChild(foot);
    dialog.addEventListener("cancel", function () { close(!!value.last); });
    document.body.appendChild(dialog);
    render();
    dialog.showModal();
  }

  function load(force) {
    fetch("/whats-new/state" + (force ? "?again=1" : ""), { headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (value) { if (value && (value.show || force)) open(value); })
      .catch(function () { /* a tour is never worth an error */ });
  }

  document.addEventListener("click", function (event) {
    var link = event.target.closest && event.target.closest("[data-whats-new]");
    if (link) { event.preventDefault(); load(true); }
  });
  setTimeout(function () { load(false); }, 1200);
})();
