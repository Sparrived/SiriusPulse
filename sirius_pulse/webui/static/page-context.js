function toElementSelector(value) {
  const text = String(value || '');
  if (/^[#.[:]/.test(text)) return text;
  return `#${text}`;
}

function emitLoadingEvent(type) {
  if (typeof window === 'undefined') return;
  window.dispatchEvent(new CustomEvent(type));
}

export function createPageContext({ container, signal, fetchImpl = globalThis.fetch } = {}) {
  const cleanups = new Set();

  function isActive() {
    return Boolean(container) && !signal?.aborted;
  }

  function addCleanup(fn) {
    if (!isActive()) return;
    cleanups.add(fn);
  }

  function cleanup() {
    for (const fn of [...cleanups]) {
      try {
        fn();
      } catch (error) {
        console.warn('page cleanup failed:', error);
      }
    }
    cleanups.clear();
  }

  signal?.addEventListener('abort', cleanup, { once: true });

  return {
    container,
    signal,
    isActive,

    $(id) {
      if (!isActive()) return null;
      return container.querySelector(toElementSelector(id));
    },

    $$(selector) {
      if (!isActive()) return [];
      return Array.from(container.querySelectorAll(selector));
    },

    query(selector) {
      if (!isActive()) return null;
      return container.querySelector(selector);
    },

    on(target, type, handler, options) {
      if (!isActive() || !target) return () => {};
      target.addEventListener(type, handler, options);
      const off = () => target.removeEventListener(type, handler, options);
      addCleanup(off);
      return off;
    },

    timeout(handler, delay) {
      if (!isActive()) return null;
      const id = setTimeout(() => {
        cleanups.delete(clear);
        if (isActive()) handler();
      }, delay);
      const clear = () => clearTimeout(id);
      addCleanup(clear);
      return id;
    },

    interval(handler, delay) {
      if (!isActive()) return null;
      const id = setInterval(() => {
        if (isActive()) handler();
      }, delay);
      const clear = () => clearInterval(id);
      addCleanup(clear);
      return id;
    },

    async fetch(url, options = {}) {
      if (!isActive()) return null;
      emitLoadingEvent('sirius:loading-begin');
      try {
        return await fetchImpl(url, { ...options, signal: options.signal || signal });
      } finally {
        emitLoadingEvent('sirius:loading-end');
      }
    },

    cleanup,
  };
}

function createInertElement() {
  const noop = () => {};
  const element = {
    addEventListener: noop,
    removeEventListener: noop,
    appendChild: noop,
    remove: noop,
    focus: noop,
    querySelector: () => element,
    querySelectorAll: () => [],
    classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
    style: {},
    dataset: {},
    value: '',
    checked: false,
    disabled: false,
    innerHTML: '',
    textContent: '',
  };
  return element;
}

export function createScopedPage() {
  let ctx = null;
  let container = null;
  const inertElement = createInertElement();

  function isActive() {
    return Boolean(ctx?.isActive?.());
  }

  return {
    use(nextCtx, fallbackContainer = null) {
      ctx = nextCtx || null;
      container = fallbackContainer;
    },

    isActive,

    $(id) {
      if (ctx) {
        if (!isActive()) return inertElement;
        return ctx.$(id);
      }
      if (!container) return null;
      return container.querySelector(toElementSelector(id));
    },

    $$(selector) {
      if (ctx) {
        if (!isActive()) return [];
        return ctx.$$(selector);
      }
      return container ? Array.from(container.querySelectorAll(selector)) : [];
    },

    query(selector) {
      if (ctx) {
        if (!isActive()) return inertElement;
        return ctx.query ? ctx.query(selector) : ctx.$$(selector)[0] || null;
      }
      return container ? container.querySelector(selector) : null;
    },

    on(target, type, handler, options) {
      if (ctx) return ctx.on(target, type, handler, options);
      if (!target) return () => {};
      target.addEventListener(type, handler, options);
      return () => target.removeEventListener(type, handler, options);
    },

    timeout(handler, delay) {
      if (ctx) return ctx.timeout(handler, delay);
      return setTimeout(handler, delay);
    },

    interval(handler, delay) {
      if (ctx) return ctx.interval(handler, delay);
      return setInterval(handler, delay);
    },
  };
}
