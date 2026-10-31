import { getToken, setToken } from './auth';
import type { EngineEvent } from './types';

type Listener = (event: EngineEvent) => void;

const listeners = new Set<Listener>();
let socket: WebSocket | null = null;
let retryTimer: number | undefined;
let attempts = 0;
let stopped = true;

export function onEngineEvent(fn: Listener): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function open(): void {
  const token = getToken();
  if (!token || stopped) return;
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws/events`, ['sirius-auth', token]);
  socket = ws;
  ws.onopen = () => {
    attempts = 0;
  };
  ws.onmessage = (msg) => {
    let event: EngineEvent;
    try {
      event = JSON.parse(msg.data);
    } catch {
      return;
    }
    listeners.forEach((fn) => fn(event));
  };
  ws.onclose = (ev) => {
    if (socket === ws) socket = null;
    if ([1008, 4001, 4003].includes(ev.code)) {
      setToken(null);
      return;
    }
    if (stopped) return;
    // 断线后由近及远地重试，带一点抖动，避免多个标签页同时挤上来。
    const delay = Math.min(30_000, 1500 * 2 ** attempts++) + Math.random() * 800;
    retryTimer = window.setTimeout(open, delay);
  };
}

export function startEvents(): () => void {
  stopped = false;
  open();
  return () => {
    stopped = true;
    window.clearTimeout(retryTimer);
    socket?.close();
    socket = null;
  };
}
