// Audio worklet: forwards 2-channel frames (0 = agent playback, 1 = microphone) to the main thread in batches.
class Capture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.left = []; this.right = []; this.count = 0;
  }
  process(inputs) {
    const input = inputs[0];
    if (input && input.length) {
      const l = input[0], r = input[1] || input[0];
      this.left.push(new Float32Array(l)); this.right.push(new Float32Array(r));
      this.count += l.length;
      if (this.count >= 2048) {
        const L = new Float32Array(this.count), R = new Float32Array(this.count);
        let o = 0;
        for (let i = 0; i < this.left.length; i++) { L.set(this.left[i], o); R.set(this.right[i], o); o += this.left[i].length; }
        this.port.postMessage([L, R], [L.buffer, R.buffer]);
        this.left = []; this.right = []; this.count = 0;
      }
    }
    return true;
  }
}
registerProcessor("capture", Capture);
