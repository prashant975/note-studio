/* QBG One - a short "What's new" tour after an update (whats_new.py).
 *
 * Asked for once on load. The server decides whether this QBG One start shows
 * it: one or two showings in all, "Got it" ends it. One step at a time; "Show
 * me" opens the place the step talks about. Never over another open dialog.
 * "What's new" under the version in the side menu opens it again any time.
 */
(function () {
  "use strict";

  var tour = null;
  var index = 0;
  var dialog = null;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
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
    if (dialog) { dialog.close(); dialog.remove(); dialog = null; }
  }

  function render() {
    var step = tour.steps[index];
    var box = dialog.querySelector("[data-tour-body]");
    box.innerHTML = "";
    var count = el("p", "small muted", "New " + (index + 1) + " of " + tour.steps.length);
    var title = el("h2", "", step.title);
    var text = el("p", "", step.text);
    box.appendChild(count); box.appendChild(title); box.appendChild(text);
    if (step.href) {
      var show = el("a", "btn sm", "Show me");
      show.href = step.href;
      show.addEventListener("click", function () { close(index === tour.steps.length - 1); });
      box.appendChild(show);
    }
    var dots = dialog.querySelector("[data-tour-dots]");
    dots.innerHTML = "";
    tour.steps.forEach(function (_s, i) { dots.appendChild(el("span", "tour-dot" + (i === index ? " on" : ""))); });
    dialog.querySelector("[data-tour-back]").disabled = index === 0;
    var next = dialog.querySelector("[data-tour-next]");
    next.textContent = index === tour.steps.length - 1 ? "Got it" : "Next";
  }

  function open(value) {
    if (!value || !value.steps || !value.steps.length || dialog) return;
    if (document.querySelector("dialog[open]")) return;
    tour = value; index = 0;
    dialog = el("dialog", "card whats-new");
    dialog.setAttribute("aria-labelledby", "whats-new-title");
    var head = el("div", "hd");
    var heading = el("h2", "", tour.title);
    heading.id = "whats-new-title";
    head.appendChild(heading);
    var bd = el("div", "bd stack");
    var body = el("div", "stack");
    body.setAttribute("data-tour-body", "");
    var dots = el("div", "tour-dots");
    dots.setAttribute("data-tour-dots", "");
    var row = el("div", "btn-row");
    var later = el("button", "btn ghost", tour.last ? "Close" : "Show me next time");
    later.type = "button";
    later.addEventListener("click", function () { close(!!tour.last); });
    var back = el("button", "btn", "Back");
    back.type = "button"; back.setAttribute("data-tour-back", "");
    back.addEventListener("click", function () { if (index > 0) { index -= 1; render(); } });
    var next = el("button", "btn primary", "Next");
    next.type = "button"; next.setAttribute("data-tour-next", "");
    next.addEventListener("click", function () {
      if (index < tour.steps.length - 1) { index += 1; render(); } else { close(true); }
    });
    row.appendChild(later); row.appendChild(back); row.appendChild(next);
    bd.appendChild(body); bd.appendChild(dots); bd.appendChild(row);
    dialog.appendChild(head); dialog.appendChild(bd);
    dialog.addEventListener("cancel", function () { close(!!tour.last); });
    document.body.appendChild(dialog);
    render();
    if (dialog.showModal) dialog.showModal(); else dialog.setAttribute("open", "");
  }

  function load(force) {
    fetch("/whats-new/state" + (force ? "?again=1" : ""), { headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (value) { if (value && (value.show || force)) open(value); })
      .catch(function () { /* a tour is never worth an error */ });
  }

  window.qbgWhatsNew = { open: function () { load(true); } };
  document.addEventListener("click", function (event) {
    var link = event.target.closest && event.target.closest("[data-whats-new]");
    if (link) { event.preventDefault(); load(true); }
  });
  /* After the page has settled, so the welcome or a toast is not covered. */
  function start() { setTimeout(function () { load(false); }, 1200); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
}());
