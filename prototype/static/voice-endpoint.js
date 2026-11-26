export const playbackEndpointOptions = {silence:.52, limit:4.5, limitFromVoice:true};

export function voiceEndpoint({silence = 1.55, limit = 25, limitFromVoice = false} = {}) {
  let elapsed = 0, firstVoice = 0, lastVoice = 0, tailGate = 0, voicedSeconds = 0, adaptiveSilence = silence;
  const background = [], peaks = [];
  return {
    get heard() { return lastVoice > 0; },
    get voicedSeconds() { return voicedSeconds; },
    get silenceSeconds() { return lastVoice ? Math.max(0, elapsed - lastVoice) : 0; },
    get currentSilence() { return adaptiveSilence; },
    get speechTiming() { return {elapsed, firstVoice, lastVoice}; },
    setAdaptiveSilence(nextSilence) {
      if (typeof nextSilence === "number" && Number.isFinite(nextSilence)) {
        adaptiveSilence = Math.max(0.35, Math.min(2.35, nextSilence));
      }
      return adaptiveSilence;
    },
    quietBlocksUntilDone(rate, samples) {
      if (!lastVoice || !samples) return 0;
      const seconds = samples / rate;
      const origin = limitFromVoice ? firstVoice : 0;
      let blocks = 0;
      for (let time = elapsed; time - lastVoice < adaptiveSilence && time - origin < limit; time += seconds) {
        blocks++;
      }
      return blocks;
    },
    push(samples, rate) {
      const duration = samples.length / rate;
      elapsed += duration;
      let sum = 0, power = 0;
      for (const sample of samples) { sum += sample; power += sample * sample; }
      const level = Math.sqrt(Math.max(0, power / samples.length - (sum / samples.length) ** 2));
      if (lastVoice) {
        if (level > tailGate) {
          lastVoice = elapsed;
          voicedSeconds += duration;
          adaptiveSilence = silence;
        }
        const origin = limitFromVoice ? firstVoice : 0;
        if (elapsed - lastVoice >= adaptiveSilence || elapsed - origin >= limit) return "done";
      } else {
        // 用近期安静段作底噪，短时能量需要连续变化才算开口。
        const ordered = background.toSorted((a,b) => a - b);
        const noise = ordered[Math.floor(ordered.length * .2)] ?? 0;
        const gate = Math.max(.00165, noise * 3);
        background.push(level);
        if (background.length > 32) background.shift();
        while (peaks.length && elapsed - peaks[0].at > .38) peaks.shift();
        if (level > gate) peaks.push({at:elapsed, level, duration});
        if (peaks.length >= 3) {
          const values = peaks.map(peak => peak.level);
          if (Math.max(...values) > 1.75 * Math.min(...values)) {
            firstVoice = Math.max(0, peaks[0].at - duration);
            lastVoice = elapsed;
            tailGate = Math.max(.00095, noise * 1.9);
            voicedSeconds = peaks.reduce((total, peak) => total + peak.duration, 0);
          }
        }
        if (!lastVoice && elapsed >= 7) return "idle";
      }
      return "";
    }
  };
}
