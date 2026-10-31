/* Installed release information and the Update button.

   The timestamp is saved at build time, never replaced with the current
   clock or the page refresh time. The Update button appears when the
   server says a newer release is listed; while an update runs, the page
   follows its progress, and when the server comes back with a different
   version the page reloads itself. */
(function () {
  "use strict";
  var panel = document.querySelector("[data-release-info]");
  if (!panel) return;
  var version = panel.querySelector("[data-release-version]");
  var updated = panel.querySelector("[data-release-updated]");
  var slot = panel.querySelector("[data-update-slot]");
  var busy = false;
  var loaded = false;
  var updatingFrom = "";

  async function getJson(url, options) {
    var controller = new AbortController();
    var timeout = setTimeout(function () { controller.abort(); }, 8000);
    try {
      var response = await fetch(url, Object.assign({ cache: "no-store", signal: controller.signal }, options || {}));
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
      var date = new Date(release.updated_at);
      if (!/^\d{4}\.\d{1,2}\.\d{1,2}\.\d+$/.test(release.version) ||
          typeof release.updated_at !== "string" || !Number.isFinite(date.getTime())) {
        throw new Error("Invalid release information");
      }
      if (updatingFrom && release.version !== updatingFrom) {
        location.reload();
        return;
      }
      version.textContent = "Version " + release.version;
      updated.dateTime = release.updated_at;
      updated.textContent = "Updated " + new Intl.DateTimeFormat("en-IN", {
        day: "numeric", month: "short", year: "numeric",
        hour: "numeric", minute: "2-digit", hour12: true, timeZoneName: "short"
      }).format(date);
      panel.title = "Installed release updated " + release.updated_at +
        ". Displayed in your local time zone.";
      loaded = true;
    } catch (_) {
      if (!loaded) version.textContent = "Version unavailable";
      panel.title = "Release information could not be refreshed. Retrying automatically.";
    } finally {
      busy = false;
    }
  }

  function show(node) {
    if (!slot) return;
    slot.innerHTML = "";
    if (node) { slot.appendChild(node); slot.hidden = false; } else { slot.hidden = true; }
  }

  function note(text) {
    var span = document.createElement("span");
    span.className = "small";
    span.textContent = text;
    return span;
  }

  /* The pop-up: once per new version, with its fixes in brief, so a teammate
     can tell at once whether the update matters to them. "Later" remembers
     the version in this browser; the header button stays either way. */
  var DISMISSED = "qbg-update-dismissed";
  var offered = "";

  function dismissed() {
    try { return localStorage.getItem(DISMISSED) || ""; } catch (_) { return ""; }
  }

  function remember(version) {
    try { localStorage.setItem(DISMISSED, version); } catch (_) { /* shown again next time; harmless */ }
  }

  function offer(status) {
    var latest = status.latest || {};
    if (!latest.version || offered === latest.version || dismissed() === latest.version) return;
    if (typeof HTMLDialogElement !== "function" || document.querySelector("dialog[open]")) return;
    offered = latest.version;
    var dialog = document.createElement("dialog");
    dialog.className = "update-dialog";
    dialog.setAttribute("aria-labelledby", "update-dialog-title");
    var form = document.createElement("form");
    form.method = "dialog";
    var title = document.createElement("h2");
    title.id = "update-dialog-title";
    title.textContent = "QBG One " + latest.version + " is ready";
    var meta = document.createElement("p");
    meta.className = "update-meta";
    meta.textContent = "You have " + (status.current || "an older version") +
      (latest.released ? ". Released " + latest.released + "." : ".");
    var list = document.createElement("ul");
    var lines = (status.notes && status.notes.length) ? status.notes : ["Fixes and improvements."];
    lines.forEach(function (line) {
      var item = document.createElement("li");
      item.textContent = line;
      list.appendChild(item);
    });
    var actions = document.createElement("div");
    actions.className = "update-actions";
    var later = document.createElement("button");
    later.className = "btn sm";
    later.value = "later";
    later.textContent = "Later";
    var now = document.createElement("button");
    now.className = "btn sm primary";
    now.value = "update";
    now.textContent = "Update now";
    actions.appendChild(later);
    actions.appendChild(now);
    form.appendChild(title);
    form.appendChild(meta);
    form.appendChild(list);
    form.appendChild(actions);
    dialog.appendChild(form);
    dialog.addEventListener("close", function () {
      var choice = dialog.returnValue;
      dialog.remove();
      if (choice === "update") { startUpdate(); } else { remember(latest.version); }
    });
    document.body.appendChild(dialog);
    dialog.showModal();
    now.focus();
  }

  function render(status) {
    var state = status.state || {};
    if (state.state === "downloading" || state.state === "installing") {
      if (!updatingFrom) updatingFrom = status.current || "";
      if (state.state === "downloading") {
        var pct = state.total ? Math.round(100 * state.done / state.total) : null;
        show(note("Downloading " + state.version + (pct === null ? "…" : " · " + pct + "%")));
      } else {
        show(note("Installing " + state.version + "… QBG One restarts itself in about a minute."));
      }
      return;
    }
    if (status.available && status.installed) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "btn sm primary";
      button.textContent = "Update to " + status.latest.version;
      button.title = status.latest.notes || "Downloads the new version, checks it and reinstalls. QBG One restarts by itself.";
      button.addEventListener("click", startUpdate);
      show(button);
      offer(status);
      return;
    }
    if (state.state === "failed" && state.error) {
      show(note("Update failed: " + state.error));
      return;
    }
    show(null);
  }

  async function refreshUpdate() {
    if (document.hidden || !slot) return;
    try {
      render(await getJson("/update/status"));
    } catch (_) {
      /* The server is away during an install; refreshRelease reloads when it is back. */
    }
  }

  async function startUpdate() {
    show(note("Starting the update…"));
    try {
      var reply = await getJson("/update/install", { method: "POST" });
      if (reply.error) { show(note("Update failed: " + reply.error)); return; }
      refreshUpdate();
    } catch (error) {
      show(note("The update could not start: " + error.message));
    }
  }

  refreshRelease();
  refreshUpdate();
  setInterval(refreshRelease, 60000);
  setInterval(refreshUpdate, 300000);
  setInterval(function () { if (updatingFrom) { refreshRelease(); refreshUpdate(); } }, 3000);
  document.addEventListener("visibilitychange", function () { refreshRelease(); refreshUpdate(); });
})();
