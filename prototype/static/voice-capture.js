class VoiceCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.block = new Float32Array(2048);
    this.count = 0;
    this.port.onmessage = () => {
      if (this.count) this.port.postMessage(this.block.slice(0, this.count));
      this.count = 0;
      this.port.postMessage("flushed");
    };
  }
  process(inputs) {
    const input = inputs[0]?.[0];
    if (input) for (const value of input) {
      this.block[this.count++] = value;
      if (this.count === this.block.length) {
        this.port.postMessage(this.block);
        this.count = 0;
      }
    }
    return true;
  }
}
registerProcessor("moss-voice-capture", VoiceCapture);
