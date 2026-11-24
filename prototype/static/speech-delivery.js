const directDistress = /(?:(?:^|[，,。！!\s])(?:我(?:今天|今儿个?|现在|最近|这会儿)?(?:真的|感觉|觉得)?)(?:不太好|不太行|不好(?:受)?)(?=$|[，,。！!\s啊呀啦呢了])|(?:不好受|不得劲儿?|没劲儿|闹心|闷得慌|堵得慌|熬(?:了个)?大?夜|眼睛.{0,4}睁不开|有点儿?崩|心态崩|啥都干不好|白忙活|又黄了|真?扫兴|挺扫兴|好扫兴|败兴|不开心|不舒服|心情(?:不(?:太)?好|差)|状态(?:不(?:太)?好|差)|累(?:坏|死|瘫|惨|垮)了?|心累|烦(?:死|透)了?|心烦|郁闷|憋屈|窝火|崩溃|忙得团团转|压力(?:好|太|很|山)?大|睡不着|失眠|头疼|头痛|太难了|扛不住|撑不住|想哭|好丧|很丧|挺丧|\bemo\b|挨.{0,4}骂|被.{0,4}骂|被.{0,4}怼|被.{0,4}批评|受气|诸事不顺))/iu;
const mildFeeling = /(?:^|[，,。！!\s])(?:我(?:今天|今儿个?|现在|最近|这会儿)?(?:真的|感觉|觉得)?|今天|今儿个?|最近|现在|这会儿)?(?:有(?:一)?点(?:儿)?|好|太|很|挺|特别|真|快)?(?:累|疲惫|难受|难过|委屈|紧张|焦虑|害怕|失落|沮丧|泄气|烦|丧)(?:极了|了|啊|呀|得很|的)?(?=$|[，,。！!\s])/u;
const negatedFeeling = /(?:不|没(?:有)?|不算|并不|一点(?:儿)?(?:也|都)?不|别|不要).{0,4}(?:累|疲惫|难受|难过|委屈|紧张|焦虑|害怕|失落|沮丧|泄气|烦|丧|压力大|崩溃|失眠|头疼|头痛|想哭|emo|郁闷|憋屈|窝火|闹心|闷得慌|堵得慌|扫兴|败兴)/iu;
const impersonalFeelingQuery = /(?:为什么|为何|什么是|啥是|怎么(?:缓解|解决|克服|消除|治疗)|原理|原因)/u;

function hasPersonalFeeling(user) {
  const plain = (user || "").trim();
  if (!plain || impersonalFeelingQuery.test(plain)) return false;
  for (const clause of plain.split(/[，,。；;！？!?\n]/u)) {
    const seg = clause.trim();
    if (!seg || negatedFeeling.test(seg)) continue;
    if (directDistress.test(seg) || mildFeeling.test(seg)) return true;
  }
  return false;
}

const zhOrdinals = [
  "", "第一", "第二", "第三", "第四", "第五", "第六", "第七", "第八", "第九", "第十",
  "第十一", "第十二", "第十三", "第十四", "第十五", "第十六", "第十七", "第十八", "第十九", "第二十",
];

function normalizeSpokenFlow(text) {
  return text
    // Strip emoji/pictographs and stage directions emitted by any cloud/local model before TTS
    .replace(/\p{Extended_Pictographic}/gu, "")
    .replace(/[（(]\s*(?:笑|轻笑|微笑|大笑|窃笑|叹气|摊手|拍拍|抱抱|挥手|眨眼|点头|摇头|鼓掌|耸肩|擦汗)\s*[）)]/gu, "")
    .replace(/\*\s*(?:笑|轻笑|微笑|大笑|叹气|摊手|拍拍|抱抱|鼓掌|耸肩)\s*\*/gu, "")
    // Convert numbered list prefixes ("1. xxx") to fluent Mandarin ordinals so TTS doesn't hard-stop on "."
    .replace(/(^|\n)\s*([1-9]|1[0-9]|20)\.\s+/gu, (_, lead, num) => `${lead}${zhOrdinals[Number(num)]}，`)
    // Convert em-dashes/double-hyphens/ellipsis into natural breath commas
    .replace(/\s*(?:——|—|--|……|\.{3,})\s*/gu, "，")
    // Soften expressive tildes ("好嘞~" -> "好嘞。") so OmniVoice doesn't stutter on "~"
    .replace(/[~～]+(?=$|\s|\n)/gu, "。")
    .replace(/[~～]+/gu, "，")
    // Strip ASCII quotes around Chinese text to prevent English token-boundary stutters
    .replace(/"([\u3400-\u9fff][^"\n]{0,40})"/gu, "$1")
    // Collapse redundant consecutive punctuation
    .replace(/[，,]{2,}/gu, "，")
    .replace(/([。！？!?])[，,]+/gu, "$1");
}

export function isVerseCategory(category) {
  return category === "verse" || category === "verse-warm";
}

function gentleRequest(user) {
  return user.replace(/《[^》]*》|“[^”]*”|「[^」]*」/gu, "").split(/[，,。；;！？!?]/u).some(clause =>
    /(?:温柔|温和|轻柔|柔和|轻声)(?:一?[些点]|地|的(?:语气|口吻|声音)|(?:朗[读诵]|吟诵|念|读|说|讲|回答))/u.test(clause) &&
    !/(?:不要|不用|无需|不必|别|不想).{0,6}(?:温柔|温和|轻柔|柔和|轻声)/u.test(clause));
}

export function explicitRecitation(user) {
  const text = (user || "").trim();
  return /^(?:请(?:你)?|麻烦(?:你)?|能不能|可以|给我|帮我|用.{1,8}的语气|(?:轻柔|温柔)地|再\s*)*(?:朗[读诵]|吟[诵咏]|诵读|背诵?|念|读)[^。！？\n]{0,42}(?:《[^》\n]+》|首(?:古诗|唐诗|宋词|诗|词)|古诗|诗词|绝句|律诗|五言|七言)/u.test(text) &&
    !/(?:不要|不必|不用|无需|别)(?:再)?(?:朗[读诵]|吟[咏诵]?|诵读|背(?:诵)?|念)|只(?:要|做)?(?:翻译|赏析|解释)/u.test(text);
}

const speechSegmentLimit = 120;
const continuationMarks = /[。！？!?；;"”’」』）】》]/u;

function splitLongPhrase(phrase) {
  const parts = [];
  for (let start = 0; start < phrase.length;) {
    let end = Math.min(start + speechSegmentLimit, phrase.length);
    if (end < phrase.length && continuationMarks.test(phrase[end])) {
      let boundary = end;
      while (boundary > start && continuationMarks.test(phrase[boundary - 1])) boundary--;
      // Keep the final spoken character with its punctuation when it straddles the limit.
      if (boundary > start + 1) end = boundary - 1;
    }
    if (end > start + 1 && /[\uDC00-\uDFFF]/u.test(phrase[end] || "")) end--;
    parts.push(phrase.slice(start, end));
    start = end;
  }
  return parts;
}

export function speechSegments(text, category) {
  const plain = text.replace(/\r\n?/g, "\n").replace(/\[([^\]]+)\]\([^)]*\)/g, "$1").replace(/https?:\/\/\S+/g, "")
    .replace(/[*#`_>|]/g, "").trim();
  const verse = isVerseCategory(category);
  const meter = /(?:^|[\n，,。；;！？!?])\s*(\p{Script=Han}{5}|\p{Script=Han}{7})\s*(?=[，,。；;！？!?\n]|$)/gu;
  const result = [];
  let body = plain;
  if (verse) {
    const lines = plain.split(/\r?\n/u);
    const title = /^《[^《》\n]{1,56}》$/u.test(lines[0].trim()) ? lines[0].trim() : "";
    const creditIndex = title ? 1 : 0;
    const original = (lines[creditIndex] || "").trim();
    const credit = original.replace(/^作者\s*[：:]\s*/u, "");
    const eraName = "先秦|两汉|西汉|东汉|三国|魏晋|南北朝|南朝|北朝|隋|唐|五代|北宋|南宋|宋|金|元|明|清|汉|晋|近现代|近代|现代|当代";
    const marked = new RegExp("^(?:[〔【（(](" + eraName + ")(?:代|朝)?[〕】）)]\\s*|(" +
      eraName + ")(?:代|朝)?[·•・\\s]+)(\\p{Script=Han}{2,8})$", "u").exec(credit);
    const bare = (title || original !== credit) && /^\p{Script=Han}{2,4}$/u.test(credit);
    const name = marked?.[3] || (bare ? credit : "");
    const separated = !lines[creditIndex+1]?.trim();
    const nextLine = (lines[creditIndex+1] || "").trim();
    const poemFollows = /^(?:\p{Script=Han}{5}|\p{Script=Han}{7})(?:[，,。！？!?]|$)/u.test(nextLine);
    if (name && (marked || original !== credit || separated || poemFollows)) {
      const era = marked && (marked[1] || marked[2]);
      const period = era && (/^[汉隋唐宋金元明清晋]$/u.test(era) ? era + "代" : era);
      const spoken = (title ? title + "。" : "") + "作者是" + (period ? period + "的" : "") + name + "。";
      result.push({caption:lines.slice(0, creditIndex+1).join("\n"), spoken});
      body = lines.slice(creditIndex+1).join("\n").trimStart();
    }
  }
  for (const paragraph of body.split(/\n\s*\n/u)) {
    const clauses = paragraph.split(/[，,。；;！？!?\n]/u).map(line => line.trim()).filter(Boolean);
    const regular = verse && clauses.length >= 2 && clauses.every(line =>
      /^(?:\p{Script=Han}{5}|\p{Script=Han}{7})$/u.test(line) && [...line].length === [...clauses[0]].length);
    let caption = "";
    const groupLimit = verse ? 68 : 92;
    const orphanLimit = verse ? 68 : 108;
    function flush() {
      if (!caption) return;
      let spoken = regular ? caption.replace(meter, (match, line) => {
        const chars = [...line], beat = chars.length === 5 ? 2 : 4;
        return match.replace(line, chars.slice(0, beat).join("") + " " + chars.slice(beat).join(""));
      }) : caption;
      if (!verse) spoken = normalizeSpokenFlow(spoken);
      spoken = spoken.replace(/([.!?]["”’]*)[ \t]*\n(?=[A-Za-z0-9])/gu, "$1 ")
        .replace(/([，。！？!?；;：:、,.…]["”’」』）】》]*)[ \t]*\n/gu, "$1")
        .replace(/([^，。！？!?；;：:、\s])\n/gu, "$1。").replace(/\n/gu, "");
      result.push({caption, spoken});
      caption = "";
    }
    // 同一段短句共用一次推理，保留句末标点和上下句语势。长段只在停顿处分组，并避免残留过短尾句。
    const phrases = paragraph.match(/[^。！？!?；;\n]+[。！？!?；;]*["”’」』）】》]*\n*|\n+/gu) || [];
    for (let pi = 0; pi < phrases.length; pi++) {
      const phrase = phrases[pi];
      const isShortTail = !verse && pi === phrases.length - 1 && phrase.trim().length <= 18;
      const limit = isShortTail ? orphanLimit : groupLimit;
      if (caption && caption.length + phrase.length > limit) flush();
      if (phrase.length > speechSegmentLimit) {
        for (const part of phrase.match(/[^，、,:：]+[，、,:：]?|[，、,:：]+/gu) || []) {
          for (const block of splitLongPhrase(part)) {
            if (caption.length + block.length > speechSegmentLimit) flush();
            caption += block;
          }
        }
      } else caption += phrase;
    }
    flush();
  }
  return result;
}

const clockStamp = /^(\d{4})年(\d{2})月(\d{2})日，星期[一二三四五六日]，(\d{2}):(\d{2}):(\d{2})。$/u;
const standaloneTimeQuestion = /^(?:(?:请|帮我|告诉我|查一下)\s*)*(?:读取本机时钟[，,]\s*(?:告诉我)?)?(?:现在|这会儿|此刻)(?:的时间|几点(?:钟)?(?:了|啦)?)[啊呀呢吗]?[？?。！!]*$/u;
const clockQuestion = /几点|(?:现在|这会儿|此刻|当前|系统|电脑|本机)的?时间/u;
const fullClockQuestion = /日期|年月日|几[号月日]|多少号|星期|周几|秒|原文|原话|原样|逐字/u;
const clockDigits = "零一二三四五六七八九";

function clockNumber(value) {
  const tens = Math.floor(value / 10), ones = value % 10;
  if (!tens) return clockDigits[ones];
  return (tens === 1 ? "十" : clockDigits[tens] + "十") + (ones ? clockDigits[ones] : "");
}

function clockReading(text, concise) {
  const stamp = clockStamp.exec(text);
  if (!stamp) return text;
  const [year, month, day, hours, minutes, seconds] = stamp.slice(1).map(Number);
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const monthLengths = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (!year || month < 1 || month > 12 || day < 1 || day > monthLengths[month - 1] ||
      hours > 23 || minutes > 59 || seconds > 59) return text;
  if (!concise) return text.replace(/\d{2}:\d{2}:\d{2}/u, `${hours}点${minutes}分${seconds}秒`);
  const period = hours < 6 ? "凌晨" : hours < 12 ? "上午" : hours === 12 ? "中午" : hours < 18 ? "下午" : "晚上";
  const hour = hours > 12 ? hours - 12 : hours;
  const minute = minutes ? `${minutes < 10 ? "零" : ""}${clockNumber(minutes)}分` : "";
  return `现在${period}${hour === 2 ? "两" : clockNumber(hour)}点${minute}。`;
}

function localReadingText({kind, reply}, conciseClock) {
  if (kind === "clock") return clockReading(reply, conciseClock);
  if (kind === "battery") {
    const reading = /^电量 (\d{1,3}%)（((?:已接电|未接电|使用电池|电源状态未知)(?:，正在充电)?)）。$/u.exec(reply);
    if (reading && Number.parseInt(reading[1], 10) <= 100) return `电量${reading[1]}，${reading[2]}。`;
  }
  if (kind === "volume") {
    const reading = /^系统音量为 (\d{1,3}%)(?:，当前(已静音|未静音))?。$/u.exec(reply);
    if (reading && Number.parseInt(reading[1], 10) <= 100) return `系统音量${reading[1]}${reading[2] ? "，" + reading[2] : ""}。`;
  }
  return reply;
}

export function speechDelivery(turn) {
  if (turn.status === "pending" && turn.error) return {category:"notice", text:turn.error};
  if (turn.status === "pending" && turn.desktop_confirmation?.action === "calendar_create") {
    const [title, slot] = turn.desktop_confirmation.detail.split("\n");
    return {category:"confirm", text:`${title}，${slot}，现在创建吗？`};
  }
  if (turn.status === "pending" && ["calendar_reschedule", "calendar_cancel"].includes(turn.desktop_confirmation?.action)) {
    const {action, detail} = turn.desktop_confirmation;
    const operation = action === "calendar_reschedule" ? "改期" : "取消";
    return {category:"confirm", text:`${detail.split("\n").join("，")}。请说“确认${operation}”继续。`};
  }
  if (turn.status === "pending" && turn.desktop_confirmation) {
    return {category:"confirm", text:"要执行“" + turn.desktop_confirmation.title + "”吗？"};
  }
  if (turn.status === "pending") return {category:"confirm", text:"草稿准备好了，现在保存吗？"};
  if (turn.status === "error") return {category:"notice", text:"这次没能完成。详情在对话里。"};
  if (turn.status === "replied" && turn.source === "notes" && turn.note_speech)
    return {category:"reply", text:turn.note_speech};
  if (turn.source === "narration" && turn.status === "replied")
    return {category:turn.narration.category, text:turn.reply, narration:turn.narration.cues};
  if (turn.status === "replied" && turn.desktop_confirmation?.action === "powershell") {
    return {category:"reply", text:"PowerShell 执行完毕，输出在对话里。"};
  }
  if (turn.status === "replied" && turn.file_move_receipt?.status === "moved") {
    return {category:"reply", text:"文件已归类，位置在对话里。"};
  }
  if (turn.status === "replied" && turn.file_plan?.status === "preview") {
    const {suggested, conflicts} = turn.file_plan;
    const count = suggested ? `建议归类${suggested}项` : "没有可直接归类的文件";
    const conflictNotice = conflicts ? `，另有${conflicts}项冲突` : "";
    return {category:"reply", text:`整理预览在对话里。${count}${conflictNotice}。`};
  }
  if (turn.status === "replied" && turn.source === "desktop" && turn.desktop_audio_muted === true)
    return {category:"reply", text:""};
  if (turn.status === "replied" && turn.local_overview?.length) {
    const user = turn.user || "";
    const conciseClock = clockQuestion.test(user) && !fullClockQuestion.test(user);
    const [first, ...rest] = turn.local_overview;
    const reading = localReadingText(first, conciseClock);
    const startsWithLocalReading = rest[0]?.kind === "weather" && reading !== first.reply;
    const text = rest.length ? reading + (startsWithLocalReading ? "\n\n" : "\n") +
      rest.map(row => localReadingText(row, conciseClock)).join("\n") : reading;
    return {category:"reply", text};
  }
  if (turn.media?.announcement) return {category:"media", text:turn.media.announcement};
  const text = turn.reply || "";
  if (turn.status === "replied" && turn.source === "holo" && turn.holo_tools?.length === 1 &&
      turn.holo_tools[0] === "get_time" && standaloneTimeQuestion.test((turn.user || "").trim()))
    return {category:"reply", text:clockReading(text, true)};
  const toolTurn = ["transport","media"].includes(turn.source);
  const unavailable = /^(?:当前页面没有可以|(?:当前媒体库|可用来源)没有找到|在可用媒体库里没有找到|所选音轨当前不可用|播放器未能|浏览器阻止|这次请求没有连接到)/u;
  if (["failed","blocked","unavailable"].includes(turn.transport?.event) || (toolTurn && unavailable.test(text)))
    return {category:"notice", text};
  if (toolTurn || turn.source === "memory" || turn.receipt || turn.call_id) return {category:"reply", text};
  if (turn.source === "smalltalk" && (text === "唉。" || text === "哈哈。")) {
    return {category:"reply", text, expression:text === "唉。" ? "sigh" : "laugh"};
  }
  const user = (turn.user || "").trim();
  const asksForVerse = /(?:朗[读诵]|吟[诵咏]|诵读|背诵|念|读|背|来|听|作|写|换).{0,14}(?:诗|词[牌作]?|绝句|律诗|五言|七言|《)|(?:诗|宋词).{0,8}(?:朗[读诵]|吟诵)/u.test(user);
  const verseLines = text.match(/(?:^|[，,。；;！？!?\n])\s*\p{Script=Han}{5}(?:\p{Script=Han}{2})?\s*(?=[，,。；;！？!?\n]|$)/gu);
  const requestPiece = /(?:背|念|读|吟|诵|来|换|听)(?:上|来)?[一这那]?[首阕]|[这那][首阕][^，。！？\n]{0,8}(?:背|念|读|吟|诵)/u.test(user);
  const recite = /朗[诵读]|吟[咏诵]|诵读|背诵/u.test(user);
  const decline = /(?:不要|不必|不用|无需|别)(?:再)?(?:朗[读诵]|吟[咏诵]?|诵读|背(?:诵)?|念)|只(?:要|做)?(?:翻译|赏析|解释)/u.test(user);
  const otherMaterial = /歌词|首歌|通知|新闻|邮件|说明书/u.test(user) && !/(?:古诗|宋词|绝句|律诗)/u.test(user);
  const lesson = /(?:怎么|如何|怎样).{0,6}(?:朗[读诵]|吟诵)|(?:朗诵|吟诵)(?:的)?(?:技巧|方法|要领)/u.test(user);
  const body = text.trim().split(/\n\s*\n/u).at(-1);
  const phrases = body.split(/[，,。；;！？!?\n]/u).map(s => s.trim()).filter(Boolean);
  const shortLines = phrases.every(s => /^\p{Script=Han}{1,12}$/u.test(s));
  const shortStanza = phrases.length >= 3 && shortLines;
  // 用户点读两句时，宋词常常没有五言或七言可以作为判断依据。
  const namesVerse = /古诗|唐诗|宋词|诗词|绝句|律诗|《[^》]+》/u.test(user);
  const excerpt = phrases.length === 2 && shortLines && namesVerse &&
    /(?:[前后头末这那]两句|首联|尾联|颔联|颈联)/u.test(user) && /[诵吟念背读]/u.test(user) &&
    !/(?:没(?:有)?找到|没看到|贴.{0,4}原文|提供.{0,4}正文)/u.test(body);
  const repeatReading = /(?:朗[诵读]|吟[诵咏]?|诵读|背诵?|念|读)(?:上)?(?:一遍|一次|一回)/u.test(user);
  const refrain = phrases.some((s, i) => s.length <= 3 && s === phrases[i - 1]);
  const metered = phrases.filter(s => /^(?:\p{Script=Han}{5}|\p{Script=Han}{7})$/u.test(s)).length;
  const wholeStanza = shortStanza && phrases.length >= 4 && (metered === phrases.length ||
    (phrases.length >= 6 && (refrain || metered >= phrases.length / 2)) ||
    /^《[^》\n]+》\s*\n(?:作者[：:]\s*)?[\p{Script=Han}·〔〕【】]{2,12}\s*\n/u.test(text.trim()));
  const labelledProseOrSong = /(?:^|\n)\s*(?:(?:歌词|作词|作曲|演唱|散文|释义|赏析|解说)[：:]|[【\[](?:主歌|副歌|chorus|verse)[\]】])/iu.test(text);
  if (!decline && !otherMaterial && !lesson && !labelledProseOrSong &&
      (((verseLines?.length >= 2 || shortStanza) && (asksForVerse || recite)) ||
       (wholeStanza && (requestPiece || repeatReading)) || excerpt))
    return {category:gentleRequest(user) ? "verse-warm" : "verse", text};
  const support = /^(?:请(?:你)?|能不能|可以|麻烦你)?(?:安慰|鼓励)(?:我|一下)/u.test(user);
  const personality = turn.personality_id || "partner";
  const warmOpener = /^(?:辛苦(?:你|您)?[了啦]|先歇一会儿|听起来你|我在[，。]|抱抱你|别硬扛|别硬撑|先缓一缓|缓一缓|缓口气|先坐下来|慢点来|真折腾|别急|出什么事|咋了|先别管|受委屈|累坏了吧|这一天|这班上得)/u.test(text);
  if (gentleRequest(user) || support || (personality !== "rational" && (hasPersonalFeeling(user) || warmOpener)))
    return {category:"warm", text};
  const request = /^(?:请|你能|能不能|可以)?(?:轻轻)?(笑一(?:声|下)|叹(?:一)?口气)(?:吗|吧)?[。？！!?]?$/u.exec(user);
  return {category:"reply", text, expression:request ? (request[1].startsWith("笑") ? "laugh" : "sigh") : undefined};
}

export function conversationSpeech(turn, history) {
  const current = speechDelivery(turn);
  if (turn.source !== "readback" || !turn.readback_of) return current;
  let previous = turn;
  let requested;
  let exactReadback = false;
  // 复读只沿原文相同、带明确编号的上一条继续找。
  for (let i = history.length - 2; previous.source === "readback" && i >= 0; i--) {
    const original = history[i];
    if (original.id !== previous.readback_of || original.reply !== previous.reply) return current;
    const words = previous.user || "";
    exactReadback ||= /原文|原话|原样|逐字/u.test(words);
    if (!requested && gentleRequest(words)) requested = "warm";
    if (!requested && /豪迈|明快|有气势/u.test(words)) requested = "reply";
    previous = original;
  }
  if (previous.source === "readback") return current;
  const origin = speechDelivery(previous);
  if (isVerseCategory(origin.category)) {
    return {...origin, category:requested === "warm" ? "verse-warm" : requested === "reply" ? "verse" : origin.category};
  }
  if (exactReadback && previous.local_overview?.length)
    return requested === "warm" ? {...current, category:"warm"} : current;
  if (origin.narration || previous.local_overview?.length)
    return {...origin, category:requested === "warm" ? "warm" : origin.category};
  return requested === "warm" ? {...current, category:"warm"} : current;
}

export function expressiveText(sentence, expression) {
  if (expression === "laugh") return sentence.replace(/^哈{2,}[，,。！!\s]*/u,"[laughter] ").trimEnd();
  if (expression === "sigh") return sentence.replace(/^[唉哎][，,。！!\s]+/u,"[sigh] ").trimEnd();
  if (/^咦[？?]/u.test(sentence)) return sentence.replace(/^咦[？?]\s*/u,"[question-yi] ");
  if (/^哇[！!]/u.test(sentence)) return sentence.replace(/^哇[！!]\s*/u,"[surprise-wa] ");
  return sentence;
}
