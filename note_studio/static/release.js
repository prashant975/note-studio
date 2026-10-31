/* NoteCraft - the installed version and the Update button beside Smart Queue.
 *
 * Copied from QBG One's release.js (QBG_Studio_Vibecraft/backend/studio/
 * qbg_studio/static/release.js, 4 Oct 2026) and placed in NoteCraft's top bar:
 *
 * - The version chip reads /static/release.json (written at build time, never
 *   replaced with the current clock) every minute.
 * - The server checks and downloads in the background, even with no browser open.
 * - The install pop-up appears only after download and verification finish.
 *   A reminder comes ten minutes after dismissal, as in QBG One.
 * - Pressing Install update starts it. The button shows download progress;
 *   when the server comes back with a different version the page reloads.
 */
(function () {
  "use strict";
  var chip = document.querySelector("[data-release-version]");
  var button = document.querySelector("[data-update-button]");
  var label = document.querySelector("[data-update-label]");
  var updated = document.querySelector("[data-release-updated]");
  if (!button || !label) return;
  var APP = button.getAttribute("data-app") || "NoteCraft";
  var DISMISSED = "notecraft-update-dismissed";
  var busy = false;
  var updatingFrom = "";
  var offered = "";
  var last = null;

  async function getJson(url, options) {
    var controller = new AbortController();
    var timeout = setTimeout(function () { controller.abort(); }, 8000);
    try {
      var response = await fetch(url, Object.assign({ cache: "no-store", signal: controller.signal,
                                                      headers: { Accept: "application/json" } }, options || {}));
      var body = await response.json();
      if (!response.ok && !body.error) throw new Error("HTTP " + response.status);
      return body;
    } finally {
      clearTimeout(timeout);
    }
  }

  async function refreshRelease() {
    if (busy || document.hidden) return;
    busy = true;
    try {
      var release = await getJson("/static/release.json");
      if (!/^\d{4}\.\d{1,2}\.\d{1,2}\.\d+$/.test(release.version)) throw new Error("Invalid release information");
      if (updatingFrom && release.version !== updatingFrom) {
        location.reload();
        return;
      }
      if (chip) chip.textContent = "v" + release.version;
      var date = new Date(release.updated_at);
      if (updated && Number.isFinite(date.getTime())) {
        updated.dateTime = release.updated_at;
        updated.textContent = "Updated " + new Intl.DateTimeFormat("en-IN", {
          day: "numeric", month: "short", year: "numeric", hour: "numeric", minute: "2-digit", hour12: true
        }).format(date);
      }
    } catch (_) {
      /* the server is away during an install; the next round tries again */
    } finally {
      busy = false;
    }
  }

  function dismissed() {
    try {
      var saved = JSON.parse(localStorage.getItem(DISMISSED) || "{}");
      return Date.now() < saved.until ? saved.version : "";
    } catch (_) { return ""; }
  }

  function remember(version) {
    offered = "";
    try { localStorage.setItem(DISMISSED, JSON.stringify({version: version, until: Date.now() + 600000})); }
    catch (_) { /* shown again next time; harmless */ }
  }

  function el(tag, cls, words) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (words) node.textContent = words;
    return node;
  }

  /* The pop-up: once per new version (or when the button is pressed on a copy
     that cannot update itself), with its fixes in brief. */
  function offer(status, asked) {
    var latest = status.latest || {};
    if (!latest.version) return;
    if (status.installed && !status.download_ready) return;
    if (!asked && (offered === latest.version || dismissed() === latest.version)) return;
    if (typeof HTMLDialogElement !== "function" || document.querySelector("dialog[open]")) return;
    offered = latest.version;
    var dialog = el("dialog", "update-dialog");
    dialog.setAttribute("aria-labelledby", "update-dialog-title");
    var form = el("form");
    form.method = "dialog";
    var title = el("h2", "", APP + " " + latest.version + " is ready");
    title.id = "update-dialog-title";
    form.appendChild(title);
    form.appendChild(el("p", "update-meta", "You have " + (status.current || "an older version") +
      (latest.released ? ". Released " + latest.released + "." : ".")));
    var list = el("ul");
    var lines = (status.notes && status.notes.length) ? status.notes : ["Fixes and improvements."];
    lines.forEach(function (line) { list.appendChild(el("li", "", line)); });
    form.appendChild(list);
    form.appendChild(el("p", "update-meta", status.installed
      ? APP + " closes, updates and opens again by itself in about a minute. Your notes, runs and settings stay."
      : "This copy runs from the project folder, so it cannot update itself. Install the new version with its installer."));
    var actions = el("div", "update-actions");
    var later = el("button", "btn sm", status.installed ? "Remind me in 10 minutes" : "Close");
    later.value = "later";
    actions.appendChild(later);
    var now = null;
    if (status.installed) {
      now = el("button", "btn sm primary", "Install update");
      now.value = "update";
      actions.appendChild(now);
    }
    form.appendChild(actions);
    dialog.appendChild(form);
    dialog.addEventListener("close", function () {
      var choice = dialog.returnValue;
      dialog.remove();
      if (choice === "update") { startUpdate(); } else { remember(latest.version); }
    });
    document.body.appendChild(dialog);
    dialog.showModal();
    (now || later).focus();
  }

  function show(words, waiting, title) {
    button.hidden = false;
    button.disabled = !!waiting;
    button.classList.toggle("busy", !!waiting);
    label.textContent = words;
    button.title = title || "";
  }

  function render(status) {
    last = status;
    var state = status.state || {};
    if (state.state === "downloading" || state.state === "installing") {
      if (state.state === "installing" && !updatingFrom) updatingFrom = status.current || "";
      if (state.state === "downloading") {
        var pct = state.total ? Math.round(100 * state.done / state.total) : null;
        show("Downloading " + (state.version || "") + (pct === null ? "…" : " · " + pct + "%"), true);
      } else {
        show("Installing " + (state.version || "") + "…", true, APP + " restarts itself in about a minute.");
      }
      return;
    }
    if (status.available && status.latest) {
      button.setAttribute("data-version", status.latest.version);
      show((status.download_ready ? "Install update " : "Update to ") + status.latest.version,
           status.installed && !status.download_ready,
           state.state === "failed" && state.error ? "The last update failed: " + state.error
             : "A newer " + APP + " is ready: press to update. " + APP + " restarts by itself.");
      if (status.download_ready) offer(status, false);
      return;
    }
    button.hidden = true;
    button.disabled = false;
    button.classList.remove("busy");
    if (chip && status.current) chip.textContent = "v" + status.current;
  }

  async function refreshUpdate() {
    if (document.hidden) return;
    try {
      render(await getJson("/update/status"));
    } catch (_) {
      /* The server is away during an install; refreshRelease reloads when it is back. */
    }
  }

  async function startUpdate() {
    if (last && !last.installed) { offer(last, true); return; }
    show("Starting the update…", true);
    try {
      var reply = await getJson("/update/install", { method: "POST" });
      if (reply.error) {
        show("Update to " + ((last && last.latest && last.latest.version) || ""), false, reply.error);
        window.alert(reply.error);
        return;
      }
      updatingFrom = (last && last.current) || "";
      refreshUpdate();
    } catch (error) {
      show("Update to " + ((last && last.latest && last.latest.version) || ""), false);
      window.alert("The update could not start: " + error.message);
    }
  }

  button.addEventListener("click", startUpdate);
  refreshRelease();
  refreshUpdate();
  setInterval(refreshRelease, 60000);
  setInterval(refreshUpdate, 15000);
  setInterval(function () { if (updatingFrom) { refreshRelease(); refreshUpdate(); } }, 3000);
  document.addEventListener("visibilitychange", function () { refreshRelease(); refreshUpdate(); });
})();
