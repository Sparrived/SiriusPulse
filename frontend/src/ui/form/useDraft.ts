import { useCallback, useEffect, useRef, useState } from 'react';
import { useLive } from '@/state/live';

export type SaveStatus = 'idle' | 'pending' | 'saving' | 'saved' | 'error';

/**
 * 可编辑的草稿：第一次拿到服务端数据（或 key 变了，比如换了伙伴）时初始化，
 * 之后以本地编辑为准，不会被后台刷新覆盖掉正在写的字。
 * ready：传入「挂载后已经重新取过一次」，避免用缓存里的旧数据起草。
 */
export function useDraft<T>(server: T | undefined, key: string | null, ready = true) {
  const [draft, setDraft] = useState<T | undefined>(undefined);
  const [baseline, setBaseline] = useState<string | null>(null);
  const seenKey = useRef<string | null>(null);

  useEffect(() => {
    if (server === undefined || !ready) return;
    if (seenKey.current === key && draft !== undefined) return;
    seenKey.current = key;
    setDraft(server);
    setBaseline(JSON.stringify(server));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [server, key, ready]);

  const patch = useCallback((change: Partial<T>) => {
    setDraft((d) => (d === undefined ? d : { ...d, ...change }));
  }, []);

  return { draft, setDraft, patch, baseline, setBaseline };
}

let lastPraise = 0;

/**
 * 自动保存：草稿与上一次保存的内容不同就在停手 delay 毫秒后保存；保存中又改了，
 * 保存完会接着再存一次。成功时她偶尔会小声说一句「记下啦」。
 */
export function useAutosave<T>(
  draft: T | undefined,
  baseline: string | null,
  setBaseline: (json: string) => void,
  save: (value: T) => Promise<unknown>,
  delay = 900,
) {
  const [status, setStatus] = useState<SaveStatus>('idle');
  const [error, setError] = useState<string | null>(null);
  const saving = useRef(false);
  const latest = useRef(draft);
  latest.current = draft;
  const saveRef = useRef(save);
  saveRef.current = save;

  const run = useCallback(async () => {
    const value = latest.current;
    if (value === undefined || saving.current) return;
    const json = JSON.stringify(value);
    saving.current = true;
    setStatus('saving');
    try {
      await saveRef.current(value);
      setBaseline(json);
      setError(null);
      setStatus('saved');
      if (Date.now() - lastPraise > 25_000) {
        lastPraise = Date.now();
        const lines = ['记下啦～', '好哦，我记住了', '嗯嗯，改好了'];
        useLive.getState().think('feel', lines[Math.floor(Math.random() * lines.length)]);
      }
    } catch (e) {
      setError((e as Error).message);
      setStatus('error');
    } finally {
      saving.current = false;
    }
  }, [setBaseline]);

  // 切走（换标签、换房间）时还有没存的改动：立刻补存，别让写了一半的字丢掉
  const baselineRef = useRef(baseline);
  baselineRef.current = baseline;
  useEffect(
    () => () => {
      const value = latest.current;
      if (value === undefined || baselineRef.current === null || saving.current) return;
      if (JSON.stringify(value) !== baselineRef.current) void saveRef.current(value).catch(() => {});
    },
    [],
  );

  // 保存期间又改了的话，这次 run 会直接返回；保存完成后 baseline 更新，
  // 这里会重新比较并再排一次。
  useEffect(() => {
    if (draft === undefined || baseline === null) return;
    if (JSON.stringify(draft) === baseline) return;
    setStatus('pending');
    const timer = window.setTimeout(run, delay);
    return () => window.clearTimeout(timer);
  }, [draft, baseline, delay, run]);

  return { status, error, retry: run };
}
