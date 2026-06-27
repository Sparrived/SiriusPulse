export function createRealtimePoller(fn, intervalMs = 3000) {
  let timer = null;
  let running = false;

  async function tick() {
    if (running) return;
    running = true;
    try {
      await fn();
    } finally {
      running = false;
    }
  }

  return {
    start() {
      this.stop();
      timer = setInterval(tick, intervalMs);
    },
    stop() {
      if (timer) clearInterval(timer);
      timer = null;
    },
    async refreshNow() {
      await tick();
    },
  };
}
