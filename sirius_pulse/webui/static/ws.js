let ws = null;
let reconnectTimer = null;

export function wsConnect() {
  if (ws && ws.readyState <= 1) return;
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  try { ws = new WebSocket(`${proto}//${location.host}/ws/events`); } catch { return; }
  
  ws.onopen = () => {
    window.dispatchEvent(new CustomEvent('ws:connected'));
    clearTimeout(reconnectTimer);
  };
  
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type !== 'connected') {
        window.dispatchEvent(new CustomEvent('sirius:event', { detail: msg }));
      }
    } catch {}
  };
  
  ws.onclose = () => {
    window.dispatchEvent(new CustomEvent('ws:disconnected'));
    reconnectTimer = setTimeout(wsConnect, 5000);
  };
  
  ws.onerror = () => ws?.close();
}

export function wsDisconnect() {
  clearTimeout(reconnectTimer);
  ws?.close();
  ws = null;
}
