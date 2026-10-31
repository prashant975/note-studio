/* NoteCraft: the few shared behaviours. No framework; HTMX does the rest.
   - top-bar menus (one open at a time; Esc, a click outside or the shade closes)
   - the phone menu sheet
   - a one-time toast from ?toast=...&kind=... (then removed from the address bar)
   - folded page guides stay folded (localStorage, best effort)
   - data-confirm on a form asks first */
(function () {
  "use strict";

  var shade = document.querySelector("[data-menu-shade]");

  function closeAll(except) {
    document.querySelectorAll("[data-menu]").forEach(function (group) {
      if (group === except) return;
      var btn = group.querySelector("[data-menu-button]");
      var panel = group.querySelector("[data-menu-panel]");
      if (btn) btn.setAttribute("aria-expanded", "false");
      if (panel) { panel.hidden = true; panel.classList.remove("open"); }
    });
    var anyOpen = document.querySelector("[data-menu-button][aria-expanded='true']");
    if (shade && !anyOpen) shade.hidden = true;
  }

  document.querySelectorAll("[data-menu]").forEach(function (group) {
    var btn = group.querySelector("[data-menu-button]");
    var panel = group.querySelector("[data-menu-panel]");
    if (!btn || !panel) return;
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      var open = btn.getAttribute("aria-expanded") === "true";
      closeAll(group);
      btn.setAttribute("aria-expanded", open ? "false" : "true");
      panel.hidden = open;
      panel.classList.toggle("open", !open);
      var wide = panel.classList.contains("gn-flyout") && window.innerWidth > 900;
      if (shade) shade.hidden = open || !wide;
    });
    panel.addEventListener("click", function (e) { e.stopPropagation(); });
  });

  document.addEventListener("click", function () { closeAll(null); });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { closeAll(null); toggleSheet(false); }
  });
  if (shade) shade.addEventListener("click", function () { closeAll(null); });

  var burger = document.querySelector("[data-nav-toggle]");
  var menu = document.getElementById("gn-menu");
  function toggleSheet(force) {
    if (!burger || !menu) return;
    var open = typeof force === "boolean" ? force : !menu.classList.contains("open");
    menu.classList.toggle("open", open);
    burger.setAttribute("aria-expanded", open ? "true" : "false");
  }
  if (burger) burger.addEventListener("click", function (e) { e.stopPropagation(); toggleSheet(); });

  /* Toasts */
  var box = document.getElementById("toasts");
  window.toast = function (text, kind) {
    if (!box || !text) return;
    var el = document.createElement("div");
    el.className = "toast " + (kind || "info");
    el.setAttribute("role", kind === "danger" ? "alert" : "status");
    var span = document.createElement("span");
    span.textContent = text;
    var x = document.createElement("button");
    x.className = "x"; x.type = "button"; x.setAttribute("aria-label", "Close"); x.textContent = "×";
    x.addEventListener("click", function () { el.remove(); });
    el.appendChild(span); el.appendChild(x);
    box.appendChild(el);
    setTimeout(function () { el.classList.add("out"); setTimeout(function () { el.remove(); }, 200); }, 6000);
  };
  try {
    var params = new URLSearchParams(window.location.search);
    if (params.has("toast")) {
      window.toast(params.get("toast"), params.get("kind") || "info");
      params.delete("toast"); params.delete("kind");
      var rest = params.toString();
      history.replaceState(null, "", window.location.pathname + (rest ? "?" + rest : "") + window.location.hash);
    }
  } catch (err) { /* old browser: no toast */ }

  /* Page guides: folded until opened, then the reader's choice is remembered. */
  document.querySelectorAll("details.page-guide[data-guide]").forEach(function (d) {
    var key = "nc-guide:" + d.getAttribute("data-guide");
    try {
      var saved = localStorage.getItem(key);
      d.open = saved === "open";
      d.addEventListener("toggle", function () { localStorage.setItem(key, d.open ? "open" : "closed"); });
    } catch (err) { /* storage blocked: leave it as rendered */ }
  });

  /* Ask before a form does something that cannot be undone; then show it is working. */
  document.addEventListener("submit", function (e) {
    var form = e.target;
    /* The pressed button may ask its own question (one form, several actions). */
    var button = e.submitter;
    var question = (button && button.getAttribute && button.getAttribute("data-confirm")) ||
      (form && form.getAttribute && form.getAttribute("data-confirm"));
    if (question && !window.confirm(question)) { e.preventDefault(); return; }
    var busy = form && form.querySelector && form.querySelector("[data-busy]");
    if (busy) busy.setAttribute("aria-busy", "true");
  });

  /* Times are stored as seconds; show them in this computer's time. */
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function when(seconds) {
    var d = new Date(seconds * 1000), now = new Date();
    var hm = ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2);
    if (d.toDateString() === now.toDateString()) return "Today " + hm;
    return d.getDate() + " " + MONTHS[d.getMonth()] + " " + hm;
  }
  function decorate(root) {
    root.querySelectorAll("[data-time]").forEach(function (el) {
      var s = parseFloat(el.getAttribute("data-time"));
      if (s) el.textContent = when(s);
    });
    /* The teacher name (and the like) is remembered on this computer. */
    root.querySelectorAll("[data-remember]").forEach(function (input) {
      var key = "nc-remember:" + input.getAttribute("data-remember");
      try {
        if (!input.value) input.value = localStorage.getItem(key) || "";
        input.addEventListener("change", function () { localStorage.setItem(key, input.value.trim()); });
      } catch (err) { /* storage blocked */ }
    });
    /* The PDF drop area: list what was chosen, and take dropped files. */
    root.querySelectorAll("[data-dropzone]").forEach(function (zone) {
      var input = zone.querySelector("[data-files]"), list = zone.querySelector("[data-file-list]");
      if (!input || zone.dataset.ready) return;
      zone.dataset.ready = "1";
      function show() {
        var names = Array.prototype.map.call(input.files || [], function (f) { return f.name; });
        list.textContent = names.length ? names.length + " chosen: " + names.join(", ") : "";
        zone.classList.toggle("has-files", names.length > 0);
      }
      input.addEventListener("change", show);
      ["dragenter", "dragover"].forEach(function (n) {
        zone.addEventListener(n, function (e) { e.preventDefault(); zone.classList.add("over"); });
      });
      ["dragleave", "drop"].forEach(function (n) {
        zone.addEventListener(n, function () { zone.classList.remove("over"); });
      });
      zone.addEventListener("drop", function (e) {
        e.preventDefault();
        if (e.dataTransfer && e.dataTransfer.files.length) { input.files = e.dataTransfer.files; show(); }
      });
    });
    /* A chosen card in a group of radio cards. */
    root.querySelectorAll(".choice-card input[type=radio]").forEach(function (radio) {
      radio.addEventListener("change", function () {
        var group = radio.closest(".choice-cards");
        group.querySelectorAll(".choice-card").forEach(function (c) { c.classList.remove("on"); });
        radio.closest(".choice-card").classList.add("on");
      });
    });
  }
  /* Sheet automation: choosing a sheet chooses its saved category. */
  var sheetSelect = document.querySelector("#run-form select[name=sheet]");
  var categorySelect = document.querySelector("#run-form select[name=category]");
  function followSheet() {
    var option = sheetSelect.options[sheetSelect.selectedIndex];
    var wanted = option && option.getAttribute("data-category");
    if (wanted && categorySelect.querySelector('option[value="' + wanted + '"]')) categorySelect.value = wanted;
  }
  if (sheetSelect && categorySelect) { sheetSelect.addEventListener("change", followSheet); followSheet(); }

  decorate(document);
  document.body.addEventListener("htmx:afterSwap", function (e) { decorate(e.target.parentNode || document); });
})();
