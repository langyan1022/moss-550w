import assert from "node:assert/strict";
import fs from "node:fs";
import {playbackEndpointOptions, voiceEndpoint} from "./static/voice-endpoint.js";
import {admitsConversationSpeech, newSpeechInPlayback, speechStopInPlayback, wakeCommand} from "./static/voice-wake.js";

const path = process.argv[2], output = process.argv[3];
const wav = fs.readFileSync(path);
const dataAt = wav.indexOf("data") + 8;
assert.equal(wav.readUInt32LE(wav.indexOf("fmt ")+12), 16000);
const input = Float32Array.from({length:(wav.length-dataAt)/2}, (_,i)=>wav.readInt16LE(dataAt+2*i)/32768);
const rate = 48000, block = 2048;
const source = Float32Array.from({length:input.length*3}, (_,i)=>{
  const a=Math.floor(i/3), f=i/3-a;
  return (1-f)*input[a]+f*(input[a+1] || 0);
});
const start=source.findIndex(x=>Math.abs(x)>.03), end=source.findLastIndex(x=>Math.abs(x)>.03);
const phrase=source.slice(start, end+1), silence=seconds=>new Float32Array(Math.round(rate*seconds));
let seed=9;
function noise(seconds, rms) {
  return silence(seconds).map(()=>{
    seed=(Math.imul(seed,1664525)+1013904223)>>>0;
    return (seed/4294967296-.5)*Math.sqrt(12)*rms;
  });
}
const rows=[];
function sample(name, parts, lastAcoustic, expected) {
  const samples=silence(26);
  let offset=0;
  for (const part of parts) { samples.set(part,offset); offset+=part.length; }
  const det=voiceEndpoint();
  let last=0, legacy, current;
  for (let i=0; i+block<=samples.length; i+=block) {
    const slice=samples.subarray(i,i+block), elapsed=(i+block)/rate;
    let power=0;
    for(const value of slice) power+=value*value;
    if(Math.sqrt(power/block)>.015) last=elapsed;
    if(!legacy && ((last>.2 && elapsed-last>1.2)||elapsed>=25)) legacy=elapsed;
    if(!current) {
      const result=det.push(slice,rate);
      if(result) current={result, elapsed};
    }
  }
  const delay=lastAcoustic == null ? null : +(current.elapsed-lastAcoustic).toFixed(3);
  const passed=current.result===expected && (delay===null || delay>=1.1 && delay<=1.9);
  rows.push({name, legacy_s:+legacy.toFixed(3), result:current.result, elapsed_s:+current.elapsed.toFixed(3), delay_s:delay,
    voiced_s:+det.voicedSeconds.toFixed(3),passed});
}
const leading=silence(.45);
for (const gain of [1,.08,.05,.035,.025]) sample("volume-"+gain,[leading,source.map(v=>v*gain)],.45+end/rate,"done");
for (const gap of [.7,1.05,1.35]) {
  const quiet=phrase.map(v=>v*.05);
  sample("pause-"+gap,[leading,quiet,silence(gap),quiet],.45+2*quiet.length/rate+gap,"done");
}
sample("blank",[],null,"idle");
sample("single-click",[leading,noise(.012,.06)],null,"idle");
sample("steady-background",[leading,noise(6,.0035)],null,"idle");
assert.ok(rows.find(row=>row.name==="volume-1").voiced_s >= .7);
assert.ok(rows.filter(row=>row.result==="idle").every(row=>row.voiced_s===0));
const blockSeconds=block/rate;
const quietBlock=new Float32Array(block);
const spokenBlocks=[];
for(let at=0;at<phrase.length;at+=block) {
  const piece=new Float32Array(block);
  piece.set(phrase.subarray(at,at+block));
  spokenBlocks.push(piece);
}
const blockPower=piece=>piece.reduce((sum,value)=>sum+value*value,0);
const sustainedBlock=spokenBlocks.reduce((best,piece)=>blockPower(piece)>blockPower(best)?piece:best);

function checkLimit(name, delay, mode) {
  const idleBlocks=Math.round(delay/blockSeconds);
  const manual=mode==="manual", short=mode==="short";
  const detector=voiceEndpoint(manual ? undefined : playbackEndpointOptions);
  for(let i=0;i<Math.ceil(30/blockSeconds);i++) {
    const voiceIndex=i-idleBlocks;
    const piece=voiceIndex<0 ? quietBlock : spokenBlocks[voiceIndex] ?? (short ? quietBlock : sustainedBlock);
    const outcome=detector.push(piece,rate);
    if(!outcome) continue;
    const {elapsed,firstVoice,lastVoice}=detector.speechTiming;
    const endOfPhrase=idleBlocks*blockSeconds+phrase.length/rate;
    const target=short ? lastVoice+.52 : manual ? 25 : firstVoice+4.5;
    const heardWholePhrase=!short || lastVoice>=endOfPhrase-blockSeconds;
    const passed=outcome==="done" && heardWholePhrase && elapsed>=target-1e-8 && elapsed<target+blockSeconds+1e-8;
    return {name,outcome,first_voice_s:firstVoice,last_voice_s:lastVoice,elapsed_s:elapsed,
      end_of_phrase_s:endOfPhrase,expected_endpoint_s:target,passed};
  }
  return {name,outcome:"timeout",passed:false};
}
const limits=[
  checkLimit("playback-late-5s",5,"short"),
  checkLimit("playback-late-6.5s",6.5,"short"),
  checkLimit("playback-continuous-4.5s",5,"continuous"),
  checkLimit("manual-session-25s",5,"manual"),
];
const names = [
  ["你好，莫斯",""], ["MOSS, play music","play music"], ["摩斯，继续。","继续。"],
  ["你好，莫斯打开灯。","打开灯。"], ["你听说过莫斯吗？",null],
  ["莫斯科今天天气如何？",null], ["摩斯密码",null], ["打开媒体库。",null],
].map(([text,expected])=>({text,passed:wakeCommand(text)===expected}));
assert.equal(newSpeechInPlayback("好到这里。",["好先到这里"]),"");
assert.equal(newSpeechInPlayback("好，先到这里。现在几点？",["好先到这里"]),"现在几点");
assert.equal(admitsConversationSpeech("し。",.166),false);
assert.equal(admitsConversationSpeech("好。",.16),true);
assert.equal(admitsConversationSpeech("天气怎么样。",.72),true);
assert.equal(speechStopInPlayback("先等等。", ["好先到这里"]), true);
assert.equal(speechStopInPlayback("先等等。", ["你可以说先等等"]), false);
assert.equal(speechStopInPlayback("别说先等等，继续播。", ["好先到这里"]), false);
const result={source:path,rows,names,limits,passed:[...rows,...names,...limits].every(row=>row.passed)};
console.log(JSON.stringify(result,null,2));
if(output) fs.writeFileSync(output,JSON.stringify(result,null,2));
assert.ok(result.passed);
