import assert from "node:assert/strict";
import {mkdirSync, readFileSync, writeFileSync} from "node:fs";
import {join, resolve} from "node:path";
import {conversationSpeech, explicitRecitation, speechDelivery, speechSegments} from "./static/speech-delivery.js";

const [chatFile, voiceFile, output] = process.argv.slice(2);
const chat = JSON.parse(readFileSync(chatFile, "utf8"));
const voice = JSON.parse(readFileSync(voiceFile, "utf8"));
assert.ok(chat.passed && voice.passed && output);
const [five, seven] = chat.cases.map(row => row.reply);
const ci = Object.values(voice.ci_audio)[0].caption;
const again = "再读一遍，读得有气势一点。";
const song = "《雨中的路》\n作词：演示\n\n背上旧行囊，路过小山岗。走进春风里，唱到天色亮。";
const prose = "我到车站时，雨已经停了。街边小店亮着灯，我把伞收起来。";
const poemOpening = "醉里挑灯看剑，梦回吹角连营。";
const report = {sources:[resolve(chatFile), resolve(voiceFile)], cases:[]};
const samples = [
  ["请朗诵王之涣的《登鹳雀楼》。", five, "verse"],
  ["背一首王之涣的。", five, "verse"],
  ["能不能给我念首李白的？", seven, "verse"],
  ["刚才那首再读一遍。", seven, "verse"],
  ["给我念首词听听。", ci, "verse"],
  ["换一阕给我听。", ci, "verse"],
  ["刚才那阕再吟一遍。", ci, "verse"],
  ["只要赏析，不要朗诵。", five, "reply"],
  ["不必背诗，给我讲讲意思。", five, "reply"],
  ["给我念首歌的歌词。", seven, "reply"],
  ["帮我读一下这份通知。", "周三开会，周四交稿，周五发布。", "reply"],
  ["古诗怎么朗诵才有抑扬顿挫？先讲方法。", "先找诗句中的停顿，再读出重音，最后连起来听一遍。", "reply"],
  ["背一首。", "想听哪一类？山水还是边塞？", "reply"],
  [again, five, "verse"],
  [again, seven, "verse"],
  [again, ci, "verse"],
  [again, song, "reply"],
  [again, prose, "reply"],
  [again, "这首词的意思是，昨夜虽只下了小雨，风却吹得很急。词人惦记着花，醒来便问身边的人。", "reply"],
  ["再轻柔一点，念一遍。", five, "verse-warm"],
  ["用温柔的语气念首词。", ci, "verse-warm"],
  ["请柔和地朗诵这首诗。", seven, "verse-warm"],
  ["不用太温柔，豪迈地再读一遍。", five, "verse"],
  ["请朗诵《温柔一点》。", seven, "verse"],
  ["请朗诵这首诗。", "我还没看到诗的正文。可以把原文贴过来吗？", "reply"],
  ["朗诵辛弃疾这首宋词的头两句。", poemOpening, "verse"],
  ["轻柔地朗诵《破阵子》的前两句。", poemOpening, "verse-warm"],
  ["把《清平乐》的末两句念给我。", "最喜小儿亡赖，溪头卧剥莲蓬。", "verse"],
  ["朗诵《破阵子》的前两句。", "没找到这首词，你能贴一下原文吗？", "reply"],
  ["读一下通知的头两句。", "今天下午停水，明天恢复供应。", "reply"],
  ["只要解释宋词的头两句。", "回想从军岁月，感慨壮志未酬。", "reply"],
];
for (const [question, reply, expected] of samples) {
  const actual = speechDelivery({status:"replied", user:question, reply}).category;
  const groups = speechSegments(reply, actual);
  const segmentText = groups.map(group => group.spoken).join("");
  const body = reply.split(/\n\s*\n/u).at(-1).replace(/\s/gu, "");
  const preserved = segmentText.replace(/\s/gu, "").endsWith(body);
  const beats = (segmentText.match(/\p{Script=Han} (?=\p{Script=Han})/gu) || []).length;
  const expectedBeats = ["verse", "verse-warm"].includes(expected) && (reply === five || reply === seven) ? 4 : 0;
  report.cases.push({question, expected, actual, beats, expectedBeats, preserved,
    passed:expected === actual && preserved && beats === expectedBeats});
}
const opening = {id:"original", user:"请朗诵辛弃疾《破阵子》的前两句。", reply:poemOpening, source:"holo"};
const reread = (id, source, user) => ({id, user, reply:source.reply, source:"readback", readback_of:source.id});
const softer = reread("soft", opening, "再轻柔一点，念一遍。");
const againSoft = reread("again", softer, "再读一遍。");
const clearer = reread("bright", againSoft, "再读一遍，读得有气势一点。");
const sequence = [opening, softer, againSoft, clearer];
report.readbacks = sequence.map((turn, index) => ({
  actual:conversationSpeech(turn, sequence.slice(0, index+1)).category,
  expected:index === 1 || index === 2 ? "verse-warm" : "verse",
}));
const plainOpening = {...opening, user:"念一下通知。"};
assert.equal(conversationSpeech(softer, [plainOpening, softer]).category, "warm");
assert.equal(conversationSpeech(againSoft, [clearer, againSoft]).category, "reply");
assert.equal(speechDelivery({user:"我这会儿挺沮丧的。别给办法。", reply:"先缓一缓，今天到这里也可以。"}).category, "warm");
assert.equal(speechDelivery({user:"用温和的语气说一下。", reply:"先从这里开始。"}).category, "warm");
assert.equal(speechDelivery({user:"“用轻柔的语气说一下”怎么翻译？", reply:"Speak gently."}).category, "reply");
report.preparation = [
  ["请朗诵杜甫《春望》的前四句。", true],
  ["温柔地朗诵一首古诗。", true],
  ["念首李清照的《如梦令》。", true],
  ["不要朗诵《春望》，给我解释。", false],
  ["《春望》怎么朗诵？", false],
  ["播放刀郎的《冲动的惩罚》", false],
].map(([user, expected]) => ({user, expected, actual:explicitRecitation(user)}));
assert.ok(report.preparation.every(row => row.expected === row.actual));
report.passed = report.cases.every(row => row.passed) && report.readbacks.every(row => row.actual === row.expected);
mkdirSync(output, {recursive:true});
writeFileSync(join(output, "result.json"), JSON.stringify(report,null,2), "utf8");
console.log(report.cases.filter(row => !row.passed).map(({question, expected, actual}) => ({question, expected, actual})));
assert.ok(report.passed, "朗诵分类、正文或节奏未通过");
