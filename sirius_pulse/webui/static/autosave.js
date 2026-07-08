export function createAutoSave({
  root,
  save,
  delay = 800,
  statusEl = null,
  onError = null,
  shouldIgnore = null,
}) {
  let timer = null;
  let saving = false;
  let queued = false;
  let ready = false;

  const setStatus = (text) => {
    if (statusEl) statusEl.textContent = text || '';
  };

  async function run() {
    timer = null;
    if (!ready) return;
    if (saving) {
      queued = true;
      return;
    }
    saving = true;
    queued = false;
    setStatus('保存中…');
    try {
      await save();
      setStatus('已自动保存');
    } catch (error) {
      setStatus('自动保存失败');
      onError?.(error);
    } finally {
      saving = false;
      if (queued) schedule();
    }
  }

  function schedule() {
    if (!ready) return;
    if (timer) clearTimeout(timer);
    setStatus('等待自动保存…');
    timer = setTimeout(run, delay);
  }

  function onChange(event) {
    if (shouldIgnore?.(event)) return;
    schedule();
  }

  root?.addEventListener('input', onChange);
  root?.addEventListener('change', onChange);

  return {
    markReady() { ready = true; },
    schedule,
    flush: run,
    destroy() {
      ready = false;
      if (timer) clearTimeout(timer);
      root?.removeEventListener('input', onChange);
      root?.removeEventListener('change', onChange);
    },
  };
}
