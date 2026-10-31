/* Execute release.js against a DOM test double; no browser or network needed. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.listeners = {};
    this.attributes = {};
    this.classList = { toggle() {}, remove() {} };
  }
  appendChild(child) { this.children.push(child); return child; }
  setAttribute(key, value) { this.attributes[key] = value; }
  getAttribute(key) { return this.attributes[key]; }
  addEventListener(type, handler) { this.listeners[type] = handler; }
  showModal() { this.open = true; }
  focus() {}
  remove() { body.children = body.children.filter(child => child !== this); }
}
const body = new Element("body");
const button = new Element("button");
const label = new Element("span");
const chip = new Element("span");
const intervals = [];
const requests = [];
const stored = new Map();
let now = 1000000;
let status = { current: "2026.10.8.1", installed: true, available: true,
  latest: { version: "2099.1.1.1" }, notes: ["Updated notes engines"],
  state: { state: "downloading", version: "2099.1.1.1", done: 50, total: 100 } };
const doc = {
  hidden: false, body,
  createElement: tag => new Element(tag),
  addEventListener() {},
  querySelector(selector) {
    if (selector === "[data-release-version]") return chip;
    if (selector === "[data-update-button]") return button;
    if (selector === "[data-update-label]") return label;
    if (selector === "dialog[open]") return body.children.find(child => child.open) || null;
    return null;
  }
};
class ClockDate extends Date { static now() { return now; } }
const context = vm.createContext({
  document: doc, HTMLDialogElement: Element, Date: ClockDate, Intl, AbortController,
  localStorage: { getItem: key => stored.get(key), setItem: (key, value) => stored.set(key, value) },
  window: { alert: message => { throw new Error(message); } },
  location: { reload() {} }, setTimeout: () => 1, clearTimeout() {},
  setInterval: (callback, period) => intervals.push({ callback, period }),
  fetch: async (url, options) => {
    requests.push({ url, method: options.method || "GET" });
    const data = url === "/update/status" ? structuredClone(status)
      : url === "/update/install" ? { ok: true }
      : { version: "2026.10.8.1", updated_at: "2026-10-08T18:00:00+05:30" };
    return { ok: true, json: async () => data };
  }
});
const settle = () => new Promise(resolve => setImmediate(resolve));
async function main() {
  vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), context);
  await settle();
  assert.equal(body.children.length, 0, "No install popup before the download finishes");
  assert.equal(button.disabled, true);
  assert.match(label.textContent, /50%/);
  assert.equal(requests.some(request => request.method === "POST"), false);
  const refresh = intervals.find(interval => interval.period === 15000).callback;
  status.state = { state: "ready", version: "2099.1.1.1" };
  status.download_ready = true;
  refresh();
  await settle();
  assert.equal(body.children.length, 1, "Verified download offers installation");
  assert.equal(button.disabled, false);
  assert.match(label.textContent, /Install update/);
  let dialog = body.children[0];
  dialog.returnValue = "later";
  dialog.listeners.close();
  refresh();
  await settle();
  assert.equal(body.children.length, 0, "Dismissal delays the reminder");
  now += 600001;
  refresh();
  await settle();
  assert.equal(body.children.length, 1, "Reminder returns after ten minutes");
  dialog = body.children[0];
  dialog.returnValue = "update";
  dialog.listeners.close();
  await settle();
  assert.equal(requests.filter(request => request.url === "/update/install").length, 1);
  console.log("Download progress, ready-only popup, reminder and explicit install passed.");
}
main().catch(error => { console.error(error); process.exitCode = 1; });
