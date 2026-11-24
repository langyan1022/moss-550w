const name = /^(?:(?:你好|您好|嘿|嗨|hello|hey)[\s，,、。！!：:]*)?(?:moss(?![a-z])|莫斯|摩斯|墨斯)/iu;

export function wakeCommand(text) {
  const value = text.trim();
  const matched = value.match(name);
  if (!matched) return null;
  const rest = value.slice(matched[0].length);
  if (/^(?:科|密码|电码)/u.test(rest)) return null;
  return rest.replace(/^[\s，,、。！!？?：:.-]+/u, "").trim();
}

export function normalizeSpoken(text) {
  return text.normalize("NFKC").replace(/[\s\p{P}]+/gu, "");
}

export function admitsConversationSpeech(text, voicedSeconds) {
  if (!text || /^(?:嗯|唔|啊)+[\s\p{P}]*$/u.test(text)) return false;
  return voicedSeconds >= .22 ||
    /^(?:好|行|对|可以|不|停|等一下|等等|继续|取消|算了|不用)$/u.test(normalizeSpoken(text));
}

export function newSpeechInPlayback(text, references) {
  if (!references.length) return text;
  const joined = references.join("");
  return text.split(/[，,。！？!?；;]+/u).map(part => part.trim()).filter(part => {
    const word = normalizeSpoken(part);
    if (word.length < 3 || joined.includes(word)) return false;
    const span = word.length <= 7 ? 2 : 3;
    let overlap = 0;
    for (let i = 0; i + span <= word.length; i++) {
      if (joined.includes(word.slice(i,i+span))) overlap++;
    }
    return overlap < (word.length - span + 1) * .52;
  }).join("，");
}

export function speechStopPhrase(text) {
  const command = wakeCommand(text) ?? text;
  const value = normalizeSpoken(command)
    .replace(/^(?:请你?|麻烦你?)/u, "").replace(/(?:吧|呀|啊|呗)$/u, "");
  return /^(?:(?:先)?(?:等一下|停一下|稍等一下|等等)|停止(?:播报|回复)|(?:先)?别(?:继续)?(?:说|播报)了|不用(?:继续)?说了|不要继续说了)$/u.test(value)
    ? value : null;
}

export function speechStopInPlayback(text, references) {
  const clear = phrase => Boolean(phrase) && !references.some(line => line.includes(phrase));
  const exact = speechStopPhrase(text);
  if (exact) return clear(exact);
  if (!references.length) return false;

  // 只有另一部分逐字出现在同时间播报中时，才把它当作可能的回声。
  const words = (wakeCommand(text) ?? text).split(/[，,。！？!?；;]+/u).map(part => part.trim()).filter(Boolean);
  const stops = words.map(speechStopPhrase);
  const commands = stops.filter(Boolean);
  if (commands.length > 1) return false;
  const inPlayback = fragment => fragment.length >= 3 && references.some(line => line.includes(fragment));
  if (clear(commands[0]) && inPlayback(normalizeSpoken(words.filter((_, index) => !stops[index]).join("")))) {
    return true;
  }
  // 识别器可能连写两个声源。前缀仍须是完整的叫停短语。
  const joined = normalizeSpoken(wakeCommand(text) ?? text);
  for (let end = 2; end <= joined.length - 3; end++) {
    if (clear(speechStopPhrase(joined.slice(0, end))) && inPlayback(joined.slice(end))) return true;
  }
  return false;
}
