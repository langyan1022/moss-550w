const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "static", "assistant-timers.js"), "utf8");
const epoch = 1_700_000_000_000;

function fixture(hash) {
  const listeners = {};
  const browser = {};
  const timers = new Map();
  const posts = [];
  const location = {hash, pathname:"/", search:"?chat=1"};
  let now = epoch;
  let handle = 0;
  class Element {
    constructor(tag) {
      this.tagName = tag.toUpperCase();
      this.children = [];
      this.dataset = {};
      this.style = {};
      this.actions = {};
    }
    setAttribute(name, value) { this[name] = value; }
    removeAttribute(name) { delete this[name]; }
    toggleAttribute(name, on) { this[name] = on; }
    append(...children) {
      for (const child of children) {
        this.children.push(child);
        child.parentElement = this;
      }
    }
    closest() { return null; }
    addEventListener(name, handler) { this.actions[name] = handler; }
  }
  const document = {
    body:new Element("body"),
    createElement:tag => new Element(tag),
    querySelector:() => null,
    addEventListener:(name, handler) => { listeners[name] = handler; }
  };
  vm.runInNewContext(source, {
    Intl, location, document,
    Date:class extends Date { static now() { return now; } },
    window:{addEventListener:(name, handler) => { browser[name] = handler; }},
    history:{replaceState:(_state, _unused, url) => { location.hash = new URL(url, "http://moss").hash; }},
    MutationObserver:class { observe() {} },
    setTimeout:(callback, delay) => {
      const ticket = ++handle;
      timers.set(ticket, {callback, at:now + delay});
      return ticket;
    },
    clearTimeout:ticket => timers.delete(ticket),
    fetch:async (url, options) => {
      posts.push([url, JSON.parse(options.body)]);
      return {ok:true};
    }
  });
  const panel = document.body.children[0];
  return {
    show(rows) { browser["moss-agent-state"]({detail:{timers:rows}}); },
    advance(ms) {
      const end = now + ms;
      for (;;) {
        const next = [...timers].sort(([, a], [, b]) => a.at - b.at)[0];
        if (!next || next[1].at > end) break;
        timers.delete(next[0]);
        now = next[1].at;
        next[1].callback();
      }
      now = end;
    },
    notice() {
      return panel.hidden ? null : {
        title:panel.children[0].textContent, label:panel.children[1].textContent
      };
    },
    visibility() { listeners.visibilitychange(); },
    dismiss() { return panel.children[3].actions.click(); },
    posts,
    location
  };
}

const reminder = (id, ms) => ({id, due_at:(epoch + ms) / 1000, label:`提醒 ${id}`});
const notice = id => ({title:"提醒时间到了", label:`提醒 ${id}`});

async function main() {
  const moved = fixture("#reminder=1");
  moved.show([reminder(2, -5000), reminder(1, -1000)]);
  assert.deepEqual(moved.notice(), notice(1), "valid Windows link keeps the clicked reminder first");
  moved.show([reminder(2, -5000), reminder(1, 120000)]);
  assert.deepEqual(moved.notice(), notice(2), "rescheduling the linked reminder reveals another due reminder");
  assert.equal(moved.location.hash, "");

  const missing = fixture("#reminder=90");
  missing.show([reminder(2, -5000)]);
  assert.deepEqual(missing.notice(), notice(2), "missing linked ID cannot mask a due reminder");
  assert.equal(missing.location.hash, "");

  const completed = fixture("#reminder=90");
  completed.show([reminder(2, 3000)]);
  assert.equal(completed.notice().title, "这条提醒已处理");
  completed.advance(2999);
  assert.equal(completed.notice().title, "这条提醒已处理");
  completed.advance(1);
  assert.deepEqual(completed.notice(), notice(2), "next timer interrupts the stale-link explanation");
  assert.equal(completed.location.hash, "");

  const beforeDue = fixture("#reminder=1");
  beforeDue.show([reminder(2, 2000), reminder(1, 120000)]);
  assert.equal(beforeDue.notice(), null);
  beforeDue.advance(1999);
  assert.equal(beforeDue.notice(), null);
  beforeDue.advance(1);
  assert.deepEqual(beforeDue.notice(), notice(2), "rescheduled link does not change the next wake time");

  const pending = fixture("#reminder=1");
  pending.visibility();
  assert.equal(pending.notice(), null, "do not report a processed reminder before the first server snapshot");
  assert.equal(pending.location.hash, "#reminder=1");
  pending.show([reminder(2, -10000), reminder(1, -1000)]);
  assert.deepEqual(pending.notice(), notice(1));
  await pending.dismiss();
  assert.deepEqual(pending.posts, [["/api/timer-dismiss", {id:1}]]);
  assert.deepEqual(pending.notice(), notice(2), "acknowledging a valid linked reminder restores timer order");
  assert.equal(pending.location.hash, "");
  console.log("5 timer notice ordering scenarios passed");
}

main().catch(error => { console.error(error); process.exitCode = 1; });
