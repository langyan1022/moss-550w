// Run with node --test verify_weather_location_timing.cjs. No browser or network needed.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { setImmediate: nextTask } = require("node:timers/promises");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "static", "app.js"), "utf8");
const settings = source.slice(source.indexOf("let weatherPosition ="), source.indexOf("function pagePlayer"));
const helpers = source.slice(source.indexOf("function asksDeviceLocation"), source.indexOf("function showError"));
const POSITION = { latitude: 34.75, longitude: 113.62 };
const WEATHER = "browserWeatherLocation('今天天气怎么样', false)";
const LOCATION = "browserWeatherLocation('我当前的定位点是在哪里，你不知道么', true)";

function client(state = "granted", permissionQuery) {
  const permission = Object.assign(new EventTarget(), { state });
  const page = new EventTarget();
  const geoCalls = [];
  const positionEvents = [];
  const lookupOrder = [];
  page.addEventListener("moss-browser-position", event => positionEvents.push(event.detail));

  const context = vm.createContext({
    AbortController, Event, clearTimeout, lookupOrder,
    setTimeout: (callback, delay) => {
      lookupOrder.push(delay);
      return setTimeout(callback, delay);
    },
    latest: { turns: [] }, window: page, weatherLocationButton: null,
    document: { getElementById: () => null },
    CustomEvent: class extends Event {
      constructor(name, { detail }) {
        super(name);
        this.detail = detail;
      }
    },
    navigator: {
      permissions: { query: async () => permissionQuery ? permissionQuery(permission) : permission },
      geolocation: { getCurrentPosition: (success, error, options) => geoCalls.push({ success, error, options }) },
    },
  });
  vm.runInContext(settings + helpers, context);
  return {
    permission, page, geoCalls, positionEvents, lookupOrder,
    eval: script => vm.runInContext(script, context),
    deliver: index => geoCalls[index].success({ coords: POSITION, timestamp: Date.now() }),
  };
}

test("audio preparation does not consume the weather lookup budget", async () => {
  const probe = client();
  const prepare = '() => lookupOrder.push("audio")';
  assert.equal(probe.eval(`browserWeatherLocation('来一首歌吧', false, ${prepare})`), null);
  assert.deepEqual(probe.lookupOrder, []);
  const weather = probe.eval(`browserWeatherLocation('今天天气怎么样', false, ${prepare})`);
  assert.deepEqual(probe.lookupOrder, ["audio", probe.eval("weatherPositionQuickWait")]);
  await nextTask();
  probe.deliver(0);
  assert.deepEqual({ ...await weather }, POSITION);
});

test("only a live, adjacent browser weather retry requests the device position", async () => {
  const probe = client();
  probe.eval(`latest.turns.push({status:'replied', weather_retry_browser_until:new Date(Date.now()+90000).toISOString()});`);
  for (const text of ["不需要", "先别重试", "需要播一首歌"]) {
    assert.equal(probe.eval(`browserWeatherLocation(${JSON.stringify(text)}, false)`), null);
  }
  const current = probe.eval("browserWeatherLocation('那就重试一遍', false)");
  await nextTask();
  assert.equal(probe.geoCalls.length, 1);
  probe.deliver(0);
  assert.deepEqual({ ...await current }, POSITION);
  probe.eval("latest.turns.at(-1).weather_retry_browser_until = new Date(Date.now()-1).toISOString();");
  assert.equal(probe.eval("browserWeatherLocation('需要', false)"), null);
  probe.eval("latest.turns.at(-1).weather_retry_browser_until = new Date(Date.now()+90000).toISOString(); latest.turns.push({status:'replied'});");
  assert.equal(probe.eval("browserWeatherLocation('需要', false)"), null);
});

test("a late fix feeds the next request while the original weather lookup returns promptly", async () => {
  const probe = client();
  const first = probe.eval(WEATHER);
  assert.equal(await first, null);
  assert.equal(probe.positionEvents.length, 0);
  assert.equal(probe.geoCalls.length, 1);
  assert.ok(probe.geoCalls[0].options.timeout > probe.eval("weatherPositionQuickWait"));

  const requestedCoordinates = probe.eval(LOCATION);
  await nextTask();
  assert.equal(probe.geoCalls.length, 1, "explicit lookup should share the active browser request");
  probe.deliver(0);
  assert.deepEqual({ ...await requestedCoordinates }, POSITION);
  assert.equal(await first, null);
  assert.equal(probe.positionEvents.length, 1);
  assert.deepEqual({ ...await probe.eval(WEATHER) }, POSITION);
  assert.equal(probe.geoCalls.length, 1, "cached fix should avoid a second geolocation call");
});

test("permission in denied or prompt state never requests coordinates automatically", async () => {
  for (const permission of ["denied", "prompt"]) {
    const probe = client(permission);
    assert.equal(await probe.eval(WEATHER), null);
    assert.equal(await probe.eval(LOCATION), null);
    assert.equal(probe.geoCalls.length, 0);
    assert.equal(probe.positionEvents.length, 0);
  }
});

test("revoked permission abandons a pending fix and discards a late browser callback", async () => {
  const probe = client();
  const result = probe.eval(LOCATION);
  await nextTask();
  assert.equal(probe.geoCalls.length, 1);
  probe.permission.state = "denied";
  probe.permission.dispatchEvent(new Event("change"));
  assert.equal(await result, null);
  probe.deliver(0);
  assert.equal(probe.positionEvents.length, 0);
  assert.equal(probe.eval("weatherPosition"), null);
});

test("leaving the page abandons both an active fix and a pending permission check", async () => {
  const active = client();
  const waiting = active.eval(LOCATION);
  await nextTask();
  assert.equal(active.geoCalls.length, 1);
  active.page.dispatchEvent(new Event("pagehide"));
  assert.equal(await waiting, null);
  active.deliver(0);
  assert.equal(active.positionEvents.length, 0);

  let finishPermission;
  const checking = client("granted", permission => new Promise(resolve => {
    finishPermission = () => resolve(permission);
  }));
  const pending = checking.eval(LOCATION);
  await nextTask();
  checking.page.dispatchEvent(new Event("pagehide"));
  finishPermission();
  assert.equal(await pending, null);
  assert.equal(checking.geoCalls.length, 0);
});
