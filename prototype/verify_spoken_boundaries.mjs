import assert from "node:assert/strict";
import {conversationSpeech, speechDelivery, speechSegments} from "./static/speech-delivery.js";

const spoken = (text, category = "reply") => speechSegments(text, category).map(part => part.spoken);
const dialogue = "可不是吗？！我也愣了一下……结果只是电话响了。";
assert.deepEqual(spoken(dialogue), [dialogue]);

const quoted = "他说：“好了？！”\r\n下一句。";
assert.deepEqual(spoken(quoted), ["他说：“好了？！”下一句。"]);
assert.deepEqual(spoken('He asked: "Ready?!"\r\nWe were.'), ['He asked: "Ready?!" We were.']);
assert.deepEqual(spoken("先看窗外\n再把灯打开。"), ["先看窗外。再把灯打开。"]);
assert.deepEqual(spoken("先回来歇一会儿。\r\n \r\n下一段。"), ["先回来歇一会儿。", "下一段。"]);

const weather = "北京今天（2026-11-20）局部多云，气温 -1.1 °C 到 8.0 °C，降水概率 0%。早晚偏冷，出门建议穿厚外套，不用带伞。";
assert.deepEqual(spoken(weather), [weather]);
assert.deepEqual(spoken("Dr. Lin checked v1.2 and got 3.5.\nThen she left."), ["Dr. Lin checked v1.2 and got 3.5. Then she left."]);

const long = "甲".repeat(119) + "？！下一句。";
const longParts = spoken(long);
assert.equal(longParts.join(""), long);
assert.ok(longParts.some(block => block.includes("甲？！")));
assert.ok(longParts.every(block => block.length <= 120 && !/^[？！]/u.test(block)));

const unicode = "甲" + "😀".repeat(78) + "？！";
const unicodeParts = spoken(unicode);
assert.equal(unicodeParts.join(""), unicode);
assert.ok(unicodeParts.every(block => block.length <= 120 && Buffer.from(block).toString("utf8") === block));

assert.deepEqual(spoken("《登鹳雀楼》\n唐·王之涣\n\n白日依山尽，黄河入海流。欲穷千里目，更上一层楼。", "verse"),
  ["《登鹳雀楼》。作者是唐代的王之涣。", "白日 依山尽，黄河 入海流。欲穷 千里目，更上 一层楼。"]);

const timestamp = "2026年11月21日，星期六，21:42:13。";
const nativeClock = {id:"clock", user:"现在几点？", source:"holo", status:"replied",
  holo_tools:["get_time"], reply:timestamp};
assert.equal(speechDelivery(nativeClock).text, "现在晚上九点四十二分。");
assert.equal(nativeClock.reply, timestamp);
for (const [time, text] of [
  ["00:05:07", "现在凌晨零点零五分。"],
  ["06:00:43", "现在上午六点。"],
  ["12:00:12", "现在中午十二点。"],
  ["14:32:07", "现在下午两点三十二分。"]
]) {
  assert.equal(speechDelivery({...nativeClock, reply:timestamp.replace("21:42:13", time)}).text, text);
}
for (const user of ["今天星期几？", "今天几号？", "现在的日期和时间？", "现在几点几分几秒？", "解释一下现在几点的概念。"]) {
  assert.equal(speechDelivery({...nativeClock, user}).text, timestamp);
}
for (const turn of [
  {...nativeClock, source:"native"},
  {...nativeClock, holo_tools:["get_time", "list_timers"]},
  {...nativeClock, reply:timestamp.replace("11月21日", "02月29日")},
  {...nativeClock, reply:timestamp.replace("21:42:13", "24:42:13")},
  {...nativeClock, reply:"本机时间暂时不可用。"}
]) {
  assert.equal(speechDelivery(turn).text, turn.reply);
}

const rows = [{kind:"clock", reply:timestamp}, {kind:"battery", reply:"电量 63%（使用电池）。"},
  {kind:"volume", reply:"系统音量为 37%，当前未静音。"}];
const reading = {id:"reading", user:"现在几点电脑还有多少电，音量多大？", source:"context",
  status:"replied", local_overview:rows, reply:rows.map(row=>row.reply).join("\n")};
const concise = "现在晚上九点四十二分。\n电量63%，使用电池。\n系统音量37%，未静音。";
assert.equal(speechDelivery(reading).text, concise);
assert.deepEqual(spoken(concise), [concise.replaceAll("\n", "")]);
assert.equal(speechDelivery({...reading, user:"今天几号、电脑还有多少电？"}).text.split("\n")[0],
  "2026年11月21日，星期六，21点42分13秒。");
const varied = { ...reading, local_overview:[{kind:"volume", reply:"系统音量为 0%，当前已静音。"},
  {kind:"battery", reply:"电量 63%（已接电，正在充电）。"}, {kind:"clock", reply:timestamp},
  {kind:"weather", reply:weather}, {kind:"memory", reply:"读取内存超时。"}] };
assert.equal(speechDelivery(varied).text, "系统音量0%，已静音。\n电量63%，已接电，正在充电。\n" +
  "现在晚上九点四十二分。\n" + weather + "\n读取内存超时。");

const repeat = {id:"repeat", user:"再念一遍。", source:"readback", status:"replied",
  readback_of:reading.id, reply:reading.reply};
assert.equal(conversationSpeech(repeat, [reading, repeat]).text, concise);
const exact = {...repeat, id:"exact", user:"把原话再念一遍。", readback_of:repeat.id};
assert.equal(conversationSpeech(exact, [reading, repeat, exact]).text, reading.reply);
const afterExact = {...repeat, id:"after-exact", readback_of:exact.id};
assert.equal(conversationSpeech(afterExact, [reading, repeat, exact, afterExact]).text, reading.reply);

const mutedByCommand = {source:"desktop", status:"replied", desktop_audio_muted:true,
  local_overview:[{kind:"volume", reply:"系统音量为 37%，当前已静音。"}]};
assert.equal(speechDelivery(mutedByCommand).text, "");
assert.equal(speechDelivery({...mutedByCommand, source:"context"}).text, "系统音量37%，已静音。");
assert.equal(speechDelivery({...mutedByCommand, desktop_audio_muted:false,
  local_overview:[{kind:"volume", reply:"系统音量为 37%，当前未静音。"}]}).text, "系统音量37%，未静音。");

console.log("10 spoken boundaries and 26 local-reading delivery checks passed");
