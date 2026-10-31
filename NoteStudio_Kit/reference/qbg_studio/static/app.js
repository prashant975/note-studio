/* QBG One - all the client-side behaviour there is.
   No framework, no build step. KaTeX and HTMX are vendored under
   /static/vendor and loaded from base.html. */

(function () {
  "use strict";

  var DELIMS = [
    { left: "\\(", right: "\\)", display: false },
    { left: "\\[", right: "\\]", display: true },
    { left: "$$", right: "$$", display: true }
  ];

  /* Render every \( ... \) inside a subtree. Safe to call repeatedly. */
  function typeset(root) {
    if (!window.renderMathInElement) return;
    try {
      window.renderMathInElement(root || document.body, {
        delimiters: DELIMS,
        throwOnError: false,
        ignoredTags: ["script", "noscript", "style", "textarea", "pre"]
      });
    } catch (e) { /* a bad formula must never take the page down */ }
  }

  /* Render one LaTeX string into a box, showing errors in place. */
  function renderInto(box, latex) {
    if (!window.katex) return;
    var src = (latex || "").trim();
    box.classList.remove("err");
    if (!src) {
      box.innerHTML = '<span class="missing">No formula yet - type one in, '
                    + 'or run Convert equations.</span>';
      return;
    }
    try {
      window.katex.render(src, box, { throwOnError: true, displayMode: false });
    } catch (e) {
      box.classList.add("err");
      box.textContent = "This formula cannot be read: " + shortErr(e);
    }
  }

  function shortErr(e) {
    var m = String(e && e.message || e);
    return m.replace(/^KaTeX parse error:\s*/, "").slice(0, 140);
  }

  /* ------------------------------------------------- live equation boxes */
  function wireEquationBoxes(root) {
    (root || document).querySelectorAll("textarea[data-latex-for]").forEach(function (ta) {
      if (ta.dataset.wired) return;
      ta.dataset.wired = "1";
      var box = document.getElementById("render-" + ta.dataset.latexFor);
      if (!box) return;
      renderInto(box, ta.value);
      var t = null;
      ta.addEventListener("input", function () {
        clearTimeout(t);
        t = setTimeout(function () { renderInto(box, ta.value); }, 160);
      });
      /* Ctrl+Enter saves without reaching for the mouse. */
      ta.addEventListener("keydown", function (ev) {
        if ((ev.ctrlKey || ev.metaKey) && ev.key === "Enter") {
          ev.preventDefault();
          var form = ta.closest("form");
          if (form) form.requestSubmit();
        }
      });
    });
  }

  /* ------------------------------------------------- keyboard shortcuts */
  function typingInField(ev) {
    var t = ev.target;
    if (!t) return false;
    var tag = (t.tagName || "").toLowerCase();
    return tag === "input" || tag === "textarea" || tag === "select"
        || t.isContentEditable;
  }

  function clickIf(sel) {
    var el = document.querySelector(sel);
    if (el && !el.disabled) { el.click(); return true; }
    return false;
  }

  document.addEventListener("keydown", function (ev) {
    if (typingInField(ev) || ev.ctrlKey || ev.metaKey || ev.altKey) return;
    var k = ev.key;
    if (k === "ArrowLeft")  { if (clickIf("[data-key=prev]")) ev.preventDefault(); return; }
    if (k === "ArrowRight") { if (clickIf("[data-key=next]")) ev.preventDefault(); return; }
    var low = (k || "").toLowerCase();
    if (low === "a") { if (clickIf("[data-key=approve]")) ev.preventDefault(); return; }
    if (low === "s") { if (clickIf("[data-key=skip]"))    ev.preventDefault(); return; }
    if (low === "f") { if (clickIf("[data-key=flag]"))    ev.preventDefault(); return; }
  });

  /* ------------------------------------------------------- confirm forms */
  /* Nothing irreversible happens without a confirm that names the count. */
  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    var msg = form && form.getAttribute("data-confirm");
    if (!msg) return;
    /* "{n}" in the message is how many boxes are ticked right now - the
       selection-table code below keeps [data-check-count] current. */
    var count = form.querySelector("[data-check-count]");
    if (count) msg = msg.replace(/\{n\}/g, count.textContent.trim());
    if (!window.confirm(msg)) { ev.preventDefault(); ev.stopPropagation(); }
  }, true);

  /* Require a known exam destination before either paper can be selected. */
  function wireCategoryUploads(root) {
    (root || document).querySelectorAll("[data-category-uploads]").forEach(function (form) {
      if (form.dataset.categoryWired) return;
      form.dataset.categoryWired = "1";
      var category = form.querySelector('[name="exam_category"]');
      var choices = Array.from(form.querySelectorAll("datalist option"), function (o) { return o.value; });
      var submit = form.querySelector("[data-category-submit]");
      var status = form.querySelector("[data-upload-category-status]");
      var hindi = form.querySelector('[name="include_hindi"]');
      var answerSource = form.querySelector('[name="answer_source"]');
      var answerHelp = form.querySelector('[data-answer-source-help]');
      var heading = form.querySelector('[data-upload-heading]');
      var originalTitle = submit.title;
      function refresh() {
        var hindiSelected = !!(hindi && hindi.checked);
        var source = hindiSelected && answerSource ? answerSource.value : 'both';
        if (answerSource) answerSource.disabled = !hindiSelected;
        form.querySelectorAll('[data-answer-source-control]').forEach(function (control) { control.hidden = !hindiSelected; });
        form.querySelectorAll('[data-answer-paper]').forEach(function (section) {
          section.hidden = hindiSelected && source !== 'both' && section.dataset.answerPaper !== source;
        });
        if (answerHelp) answerHelp.textContent = source === 'both'
          ? 'Upload the separate English and Hindi answer keys and worked solutions.'
          : 'The ' + (source === 'hindi' ? 'Hindi' : 'English') + ' file supplies correct answers for matching questions in both languages. '
            + (source === 'hindi' ? 'English' : 'Hindi') + ' worked solutions are left empty. Review the option order in both papers.';
        if (hindi) {
          form.dataset.paperCount = hindiSelected ? (source === 'both' ? "4" : "3") : "2";
          form.querySelectorAll('[data-hindi-papers]').forEach(function (section) { section.hidden = !hindiSelected; });
          hindi.setAttribute('aria-expanded', String(hindiSelected));
          if (heading) heading.textContent = 'Choose the ' + ({'2':'two','3':'three','4':'four'}[form.dataset.paperCount]) + ' Word files';
        }
        var valid = choices.indexOf(category.value.trim()) !== -1;
        category.setCustomValidity(valid ? "" : "Select an exam category from the list first.");
        form.querySelectorAll('.drop input[type=file]').forEach(function (input) {
          input.disabled = !valid || !!(hindi && (input.name || '').indexOf('hindi_') === 0 && !hindiSelected)
            || (hindiSelected && input.name === 'answers_docx' && source === 'hindi')
            || (hindiSelected && input.name === 'hindi_answers_docx' && source === 'english');
        });
        form.querySelectorAll('.drop').forEach(function (zone) {
          var locked = zone.querySelector('input[type=file]').disabled;
          zone.setAttribute("aria-disabled", String(locked));
          zone.setAttribute("tabindex", locked ? "-1" : "0");
          if (locked) zone.classList.remove("over");
        });
        submit.disabled = !valid || form.dataset.extractReady !== "1";
        submit.title = valid ? originalTitle : "Select an exam category first";
        status.textContent = valid
          ? "Category selected: " + category.value.trim() + (form.dataset.paperCount === "4" ? ". You can now choose all four papers." : form.dataset.paperCount === "3" ? ". You can now choose the three papers." : ". You can now choose both papers.")
          : "Select an exam category above to unlock the paper uploads.";
        return valid;
      }
      category.addEventListener("input", refresh);
      category.addEventListener("change", refresh);
      if (hindi) hindi.addEventListener('change', refresh);
      if (answerSource) answerSource.addEventListener('change', refresh);
      form.addEventListener("submit", function (ev) {
        if (!refresh()) {
          ev.preventDefault();
          ev.stopImmediatePropagation();
          category.reportValidity();
        }
      });
      refresh();
    });
  }

  /* ------------------------------------------------------------ dropzones */
  function wireDrops(root) {
    (root || document).querySelectorAll(".drop").forEach(function (zone) {
      if (zone.dataset.wired) return;
      zone.dataset.wired = "1";
      var input = zone.querySelector("input[type=file]");
      var out = zone.querySelector(".chosen");
      if (!input) return;

      function show() {
        if (!out) return;
        var f = input.files && input.files[0];
        if (!f) { out.textContent = ""; return; }
        if (!/\.docx$/i.test(f.name)) {
          out.classList.add("bad");
          out.textContent = "That is not a Word .docx file: " + f.name;
        } else {
          out.classList.remove("bad");
          out.textContent = "Chosen: " + f.name;
        }
      }
      zone.setAttribute("role", "button");
      if (!zone.hasAttribute("tabindex")) zone.setAttribute("tabindex", "0");
      /* It is announced as a button, so it needs a name. The visible heading
         inside the zone is that name. */
      if (!zone.hasAttribute("aria-label")) {
        var lbl = zone.querySelector(".lbl");
        zone.setAttribute("aria-label",
          "Choose the " + ((lbl && lbl.textContent.trim()) || "file"));
      }
      zone.addEventListener("click", function (ev) {
        if (input.disabled) return;
        if (ev.target === input) return;      /* the input's own click bubbling back */
        input.click();
      });
      zone.addEventListener("keydown", function (ev) {
        if (input.disabled) return;
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); input.click(); }
      });
      input.addEventListener("change", show);
      ["dragenter", "dragover"].forEach(function (e) {
        zone.addEventListener(e, function (ev) {
          ev.preventDefault();
          if (!input.disabled) zone.classList.add("over");
        });
      });
      ["dragleave", "drop"].forEach(function (e) {
        zone.addEventListener(e, function (ev) {
          ev.preventDefault(); zone.classList.remove("over");
        });
      });
      zone.addEventListener("drop", function (ev) {
        if (!input.disabled && ev.dataTransfer && ev.dataTransfer.files.length) {
          input.files = ev.dataTransfer.files;
          show();
        }
      });
    });
  }

  /* ---------------------------------------------------------------- boot */
  /* ------------------------------------------------- selection tables */
  /* data-check-all="foo"    a checkbox driving every enabled
                             input[name=foo] in the same form
     data-check-count="foo"  an element whose text is how many are ticked */

  function boxesFor(form, name) {
    if (!form) return [];
    return Array.prototype.slice.call(
      form.querySelectorAll('input[type=checkbox][name="' + name + '"]')
    ).filter(function (b) { return !b.disabled; });
  }

  function refreshCounts(form, name) {
    var boxes = boxesFor(form, name);
    var on = boxes.filter(function (b) { return b.checked; }).length;

    form.querySelectorAll('[data-check-count="' + name + '"]').forEach(
      function (el) { el.textContent = String(on); });

    /* Nothing ticked means nothing to send - say so on the button rather
       than letting the server bounce it back. */
    form.querySelectorAll("button[type=submit]").forEach(function (btn) {
      if (!boxes.length) return;          /* server already disabled it */
      btn.disabled = on === 0;
      if (on === 0) btn.title = "Tick at least one question first";
      else btn.removeAttribute("title");
    });

    form.querySelectorAll('[data-check-all="' + name + '"]').forEach(
      function (master) {
        master.checked = on > 0 && on === boxes.length;
        master.indeterminate = on > 0 && on < boxes.length;
      });
  }

  function wireSelectionTables(root) {
    (root || document).querySelectorAll("[data-check-all]").forEach(
      function (master) {
        if (master.dataset.wired) return;
        master.dataset.wired = "1";
        var name = master.getAttribute("data-check-all");
        var form = master.closest("form");
        if (!form) return;

        master.addEventListener("change", function () {
          boxesFor(form, name).forEach(function (b) {
            b.checked = master.checked;
          });
          refreshCounts(form, name);
        });

        form.addEventListener("change", function (ev) {
          if (ev.target && ev.target.name === name) refreshCounts(form, name);
        });

        refreshCounts(form, name);
      });
  }

  /* Long dropdowns use one combined searchable control. The original select
     remains the submitted field, but is hidden; this keeps every route and
     dependent change listener working with an exact approved option value. */
  function wireSearchableSelects(root) {
    (root || document).querySelectorAll("select[data-searchable]").forEach(function (select) {
      if (select.dataset.searchWired) return;
      select.dataset.searchWired = "1";
      var id = "select-list-" + Math.random().toString(36).slice(2);
      var box = document.createElement("input");
      box.type = "text";
      box.className = "select-combobox";
      box.autocomplete = "off";
      box.placeholder = select.getAttribute("data-searchable") || "Type to search";
      box.setAttribute("aria-label", box.placeholder);
      box.setAttribute("role", "combobox");
      box.setAttribute("aria-autocomplete", "list");
      box.setAttribute("aria-expanded", "false");
      box.setAttribute("list", id);
      var choices = document.createElement("datalist");
      choices.id = id;
      Array.prototype.forEach.call(select.options, function (option) {
        if (!option.value) return;
        var choice = document.createElement("option");
        choice.value = option.text.trim();
        choices.appendChild(choice);
      });
      select.parentNode.insertBefore(box, select);
      select.parentNode.insertBefore(choices, select);
      var wasRequired = select.required;
      select.required = false;
      select.classList.add("select-combobox-source");
      select.tabIndex = -1;
      select.setAttribute("aria-hidden", "true");
      box.required = wasRequired;

      function norm(text) { return String(text || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim(); }
      function match() {
        var wanted = norm(box.value);
        return Array.prototype.filter.call(select.options, function (option) {
          return option.value && norm(option.text) === wanted;
        })[0] || null;
      }
      function syncToSelect() {
        var chosen = match();
        var value = chosen ? chosen.value : "";
        box.setCustomValidity(!box.value && wasRequired ? "Choose an option."
          : box.value && !chosen ? "Choose a value from the suggestions." : "");
        if (select.value !== value) {
          select.value = value;
          select.dispatchEvent(new Event("change", { bubbles: true }));
        }
      }
      function syncFromSelect() {
        var chosen = select.selectedOptions[0];
        box.value = chosen && chosen.value ? chosen.text.trim() : "";
        box.setCustomValidity("");
      }
      syncFromSelect();
      box.addEventListener("input", syncToSelect);
      box.addEventListener("change", syncToSelect);
      box.addEventListener("focus", function () { box.setAttribute("aria-expanded", "true"); });
      box.addEventListener("blur", function () { box.setAttribute("aria-expanded", "false"); });
      select.addEventListener("change", function () {
        var chosen = select.selectedOptions[0];
        if (chosen && chosen.value && norm(box.value) !== norm(chosen.text)) syncFromSelect();
      });
    });
  }

  function boot(root) {
    typeset(root);
    wireEquationBoxes(root);
    wireSearchableSelects(root);
    wireCategoryUploads(root);
    wireDrops(root);
    wireSelectionTables(root);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { boot(document.body); });
  } else {
    boot(document.body);
  }

  /* HTMX swaps in new markup - re-run everything over just that fragment. */
  document.body.addEventListener("htmx:afterSwap", function (ev) {
    boot(ev.target);
  });
  document.body.addEventListener("htmx:afterSettle", function (ev) {
    typeset(ev.target);
  });

  /* A request that fails swaps nothing: HTMX leaves a 4xx/5xx answer and a
     dropped connection alone, so the indicator simply stopped and the screen
     looked as if the click had not registered - and the operator pressed
     again. Say so, and give the button back. Once per few seconds: a poller
     that keeps failing must not stack a toast every tick. */
  var lastFailToast = 0;
  function requestFailed(ev, message) {
    var elt = ev.detail && ev.detail.elt;
    if (elt && elt.querySelectorAll) {
      var busy = Array.prototype.slice.call(elt.querySelectorAll('[aria-busy="true"]'));
      if (elt.getAttribute("aria-busy") === "true") busy.push(elt);
      busy.forEach(function (b) { b.removeAttribute("aria-busy"); b.disabled = false; });
    }
    var now = Date.now();
    if (now - lastFailToast < 8000) return;
    lastFailToast = now;
    toast(message, "danger");
  }
  document.body.addEventListener("htmx:responseError", function (ev) {
    var status = (ev.detail && ev.detail.xhr && ev.detail.xhr.status) || 0;
    requestFailed(ev, "That did not go through" + (status ? " (error " + status + ")" : "")
                      + ". Reload the page and try again.");
  });
  document.body.addEventListener("htmx:sendError", function (ev) {
    requestFailed(ev, "QBG One did not answer. Check it is still running, "
                      + "then reload the page and try again.");
  });

  /* ------------------------------------------------------- mobile drawer */
  /* Under 900px the sidebar is off-canvas. Opening it traps nothing and
     closes on Escape, on a link, or on the backdrop - a drawer you cannot
     get out of is worse than no drawer. */
  function closeNav() {
    document.body.classList.remove("nav-open");
    var b = document.querySelector("[data-nav-toggle]");
    if (b) b.setAttribute("aria-expanded", "false");
  }
  document.addEventListener("click", function (ev) {
    var toggle = ev.target.closest && ev.target.closest("[data-nav-toggle]");
    if (toggle) {
      ev.preventDefault();
      var open = document.body.classList.toggle("nav-open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      return;
    }
    if (!document.body.classList.contains("nav-open")) return;
    /* a link inside the drawer, or the backdrop */
    if (ev.target.closest && ev.target.closest(".sidebar a")) { closeNav(); return; }
    if (!ev.target.closest || !ev.target.closest(".sidebar")) closeNav();
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") closeNav();
  });

  /* -------------------------------------------------------------- toasts */
  /* Short, factual, and never for something already visible on the page. */
  var ICONS = {
    ok: '<path d="M20 6 9 17l-5-5"/>',
    warn: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4M12 17h.01"/>',
    danger: '<circle cx="12" cy="12" r="9"/><path d="M12 8v4M12 16h.01"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/>'
  };

  function toast(message, kind, ms) {
    var host = document.getElementById("toasts");
    if (!host || !message) return;
    kind = kind || "info";
    var el = document.createElement("div");
    el.className = "toast " + kind;
    el.setAttribute("role", kind === "danger" ? "alert" : "status");
    el.innerHTML =
      '<svg class="tico" width="16" height="16" viewBox="0 0 24 24" fill="none" '
      + 'stroke="currentColor" stroke-width="1.75" stroke-linecap="round" '
      + 'stroke-linejoin="round" aria-hidden="true">' + (ICONS[kind] || ICONS.info)
      + '</svg><div class="grow"></div>'
      + '<button class="x" type="button" aria-label="Dismiss">&times;</button>';
    el.querySelector(".grow").textContent = message;
    host.appendChild(el);
    var timer = setTimeout(dismiss, ms || 6000);
    function dismiss() {
      clearTimeout(timer);
      el.classList.add("out");
      setTimeout(function () { el.remove(); }, 180);
    }
    el.querySelector(".x").addEventListener("click", dismiss);
  }

  /* The server hands a message over on a redirect (?toast=...&kind=ok) and
     it is stripped from the address bar straight away, so a refresh does not
     replay a message about something that already happened. */
  function toastFromUrl() {
    var q = new URLSearchParams(window.location.search);
    var msg = q.get("toast");
    if (!msg) return;
    toast(msg, q.get("kind") || "info");
    q.delete("toast"); q.delete("kind");
    var rest = q.toString();
    history.replaceState({}, "",
      window.location.pathname + (rest ? "?" + rest : "") + window.location.hash);
  }

  /* --------------------------------------------------- button busy state */
  /* A submitted form must not look idle. The button reports itself busy
     until the page navigates away, so nobody presses Run twice. */
  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    if (!form || form.hasAttribute("data-no-busy")) return;
    if (form.getAttribute("hx-post") || form.getAttribute("hx-get")) return;
    /* The submitter, when the browser reports it; else the first submit
       button. A form with two actions must mark the one that was pressed. */
    var btn = (ev.submitter && ev.submitter.tagName === "BUTTON") ? ev.submitter
            : form.querySelector('button[type=submit], button:not([type])');
    if (!btn || btn.disabled) return;
    setTimeout(function () {
      btn.setAttribute("aria-busy", "true");
      btn.disabled = true;
    }, 0);
  });

  /* ---------------------------------------------------------------- boot */
  toastFromUrl();

  window.QBG = { typeset: typeset, renderInto: renderInto, toast: toast };
})();

/* "Each file is a separate paper" (Autopilot, New batch): say how many batches the chosen files become. */
(function () {
  document.querySelectorAll('[data-separate-papers]').forEach(function (box) {
    var form = box.closest('form');
    var files = form && form.querySelector('[data-paper-files]');
    var out = form && form.querySelector('[data-separate-count]');
    if (!files || !out) return;
    function say() {
      var n = files.files ? files.files.length : 0;
      out.textContent = n < 2 ? '' : box.checked
        ? '(' + n + ' files: ' + n + ' separate papers, ' + n + ' batches)'
        : '(' + n + ' files: read together as ONE paper)';
    }
    files.addEventListener('change', say); box.addEventListener('change', say); say();
  });
}());

/* The short guide on top of a main screen (guides.py): open the first time a screen is
   visited, folded after that - one click on "How this works" brings it back. */
(function () {
  document.querySelectorAll('[data-page-guide]').forEach(function (box) {
    var key = 'qbg-guide-seen:' + box.getAttribute('data-page-guide');
    try {
      if (window.localStorage.getItem(key) !== '1') {
        box.setAttribute('open', '');
        window.localStorage.setItem(key, '1');
      }
    } catch (e) { /* storage may be off: the guide stays folded, one click away */ }
  });
}());

/* Search questions: carry the ticked IDs (or every ID shown) to a task's form, or copy them. */
(function () {
  var KEY = 'qbg-prefill-ids';
  function chosen() {
    var all = Array.prototype.slice.call(document.querySelectorAll('[data-result-id]'));
    var ticked = all.filter(function (box) { return box.checked; });
    return (ticked.length ? ticked : all).map(function (box) { return box.value; });
  }
  function say(text) {
    var out = document.querySelector('[data-ids-said]');
    if (out) out.textContent = text;
  }
  document.addEventListener('click', function (event) {
    var send = event.target.closest && event.target.closest('[data-send-ids]');
    var copy = event.target.closest && event.target.closest('[data-copy-ids]');
    if (!send && !copy) return;
    var ids = chosen();
    if (!ids.length) { event.preventDefault(); return; }
    if (send) {
      try { window.sessionStorage.setItem(KEY, ids.join('\n')); } catch (e) { /* the form opens empty */ }
      return;
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(ids.join('\n')).then(function () { say(ids.length + ' IDs copied.'); },
                                                         function () { say('Could not copy; select the IDs by hand.'); });
    }
  });
  /* The hint under the search box follows the way chosen. */
  document.addEventListener('change', function (event) {
    if (!event.target.matches || !event.target.matches('[data-search-mode]')) return;
    Array.prototype.forEach.call(document.querySelectorAll('[data-mode-hint]'), function (hint) {
      hint.hidden = hint.getAttribute('data-mode-hint') !== event.target.value;
    });
  });
  /* On the task's form: fill its IDs box once with what Search sent. */
  var box = document.querySelector('textarea[name="ids"], textarea[name="lines"]');
  if (!box || box.value) return;
  try {
    var saved = window.sessionStorage.getItem(KEY);
    if (saved) { box.value = saved; window.sessionStorage.removeItem(KEY); }
  } catch (e) { /* storage may be off */ }
}());
