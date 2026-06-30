import { store } from '../store.js';

const TYPE_RESOURCES = {
  perception_completed: ['conversations', 'logs', 'monitoring', 'dashboard'],
  cognition_completed: ['cognition', 'conversations', 'tokens', 'logs', 'monitoring', 'dashboard'],
  decision_completed: ['cognition', 'logs', 'monitoring', 'dashboard'],
  execution_completed: ['conversations', 'tokens', 'logs', 'monitoring', 'dashboard'],
  delayed_response_triggered: ['conversations', 'logs', 'monitoring', 'dashboard'],
  reminder_triggered: ['conversations', 'logs', 'monitoring', 'dashboard'],
  local_change: ['dashboard'],
};

function asArray(value) {
  if (!value) return [];
  return Array.isArray(value) ? value : [value];
}

function resourcesForEvent(detail) {
  const explicit = asArray(detail?.resources || detail?.resource);
  if (explicit.length) return explicit;
  return TYPE_RESOURCES[detail?.type] || [];
}

function personaMatches(detail, personaScoped) {
  if (!personaScoped) return true;
  const eventPersona = String(detail?.persona || '').trim();
  if (!eventPersona || eventPersona === '*') return true;
  return eventPersona === store.currentPersona;
}

export function createRealtimeRefresh(fn, options = {}) {
  const resourceSet = new Set(asArray(options.resources));
  const eventTypeSet = new Set(asArray(options.eventTypes));
  const debounceMs = Number(options.debounceMs ?? 300);
  const personaScoped = options.personaScoped !== false;
  const shouldRefresh = options.shouldRefresh || (() => true);

  let active = false;
  let timer = null;
  let running = false;
  let queued = false;

  function matches(detail) {
    if (!personaMatches(detail, personaScoped)) return false;
    if (eventTypeSet.size && eventTypeSet.has(detail?.type)) return true;
    if (!resourceSet.size) return true;
    return resourcesForEvent(detail).some((resource) => resourceSet.has(resource));
  }

  async function run(silent = true) {
    if (!active || !shouldRefresh()) return;
    if (running) {
      queued = true;
      return;
    }
    running = true;
    try {
      await fn(silent);
    } finally {
      running = false;
      if (queued) {
        queued = false;
        schedule(true);
      }
    }
  }

  function schedule(silent = true) {
    if (!active || !shouldRefresh()) return;
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      run(silent);
    }, debounceMs);
  }

  function onEvent(event) {
    if (matches(event.detail || {})) schedule(true);
  }

  function onConnected() {
    schedule(true);
  }

  return {
    start() {
      if (active || typeof window === 'undefined') return;
      active = true;
      window.addEventListener('sirius:event', onEvent);
      window.addEventListener('ws:connected', onConnected);
    },
    stop() {
      active = false;
      if (timer) clearTimeout(timer);
      timer = null;
      if (typeof window === 'undefined') return;
      window.removeEventListener('sirius:event', onEvent);
      window.removeEventListener('ws:connected', onConnected);
    },
    refreshNow(silent = true) {
      return run(silent);
    },
  };
}
