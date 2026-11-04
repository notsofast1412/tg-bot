const flush = () => new Promise(resolve => setImmediate(resolve));

class FakeClock {
  constructor() {
    this.now = 1700000000000;
    this.next = 1;
    this.timers = new Map();
    const clock = this;
    this.Date = class extends Date {
      constructor(...args) { super(...(args.length ? args : [clock.now])); }
      static now() { return clock.now; }
    };
    this.setTimeout = (fn, ms = 0) => {
      const id = this.next++;
      this.timers.set(id, {at: this.now + Math.max(0, ms), fn});
      return id;
    };
    this.clearTimeout = id => this.timers.delete(id);
  }
  async advance(ms) {
    await flush();
    const target = this.now + ms;
    for (;;) {
      const next = [...this.timers].filter(([, task]) => task.at <= target).sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
      if (!next) break;
      this.now = next[1].at;
      this.timers.delete(next[0]);
      next[1].fn();
      await flush();
    }
    this.now = target;
    await flush();
  }
}
module.exports = {FakeClock, flush};
