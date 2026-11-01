import { useEffect, useMemo, useRef, useState } from 'react';
import { fetchLogs } from '@/lib/queries-house';
import { Segmented } from '@/ui/form/fields';

const KEEP = 3000;

function level(line: string): string {
  if (/\b(ERROR|CRITICAL|Traceback)\b/.test(line)) return 'error';
  if (/\bWARN(ING)?\b/.test(line)) return 'warn';
  if (/\bDEBUG\b/.test(line)) return 'debug';
  return '';
}

/** 日志：房子里的「监控录像」，2 秒续一次，最多留最近 3000 行 */
export function Logs({ persona }: { persona: string }) {
  const [source, setSource] = useState<'persona' | 'webui'>('persona');
  const [lines, setLines] = useState<string[]>([]);
  const [paused, setPaused] = useState(false);
  // 暂停只是不再往下读，屏幕上已有的不清掉：用 ref 而不是让 effect 重跑
  const pausedRef = useRef(false);
  pausedRef.current = paused;
  const [filter, setFilter] = useState('');
  const [info, setInfo] = useState<{ path: string; exists: boolean } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const offset = useRef(0);
  const box = useRef<HTMLPreElement>(null);
  const stick = useRef(true);

  useEffect(() => {
    setLines([]);
    offset.current = 0;
    let cancelled = false;
    let timer: number | undefined;
    const tick = async (first: boolean) => {
      try {
        if (!pausedRef.current || first) {
          const chunk = await fetchLogs(source, offset.current, first ? 300 : 2000, persona);
          if (cancelled) return;
          setInfo({ path: chunk.path, exists: chunk.exists });
          setError(null);
          // 文件被轮转/截断时偏移量会变小：从头接上
          if (chunk.offset < offset.current) setLines([]);
          offset.current = chunk.offset;
          if (chunk.lines.length) setLines((prev) => [...prev, ...chunk.lines].slice(-KEEP));
        }
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      }
      if (!cancelled) timer = window.setTimeout(() => tick(false), 2000);
    };
    void tick(true);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [source, persona]);

  useEffect(() => {
    if (stick.current && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [lines]);

  const shown = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return q ? lines.filter((l) => l.toLowerCase().includes(q)) : lines;
  }, [lines, filter]);

  return (
    <section className="sheet paper logs">
      <div className="logs__bar">
        <Segmented
          size="sm"
          value={source}
          onChange={setSource}
          options={[
            { value: 'persona', label: '她的日志' },
            { value: 'webui', label: '小屋的日志' },
          ]}
        />
        <input
          className="input logs__filter"
          placeholder="只看包含……"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        <button className="soft-btn ghost" onClick={() => setPaused((v) => !v)}>
          {paused ? '继续' : '暂停'}
        </button>
        <button className="soft-btn ghost" onClick={() => setLines([])}>
          清屏
        </button>
      </div>
      {error && <p className="star-card__error">{error}</p>}
      <pre
        ref={box}
        className="logs__screen"
        onScroll={(e) => {
          const el = e.currentTarget;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
        }}
      >
        {shown.length
          ? shown.map((l, i) => (
              <span key={i} className={`logs__line logs__line--${level(l)}`}>
                {l}
                {'\n'}
              </span>
            ))
          : info && !info.exists
            ? '还没有日志文件。'
            : '安安静静的，什么也没发生。'}
      </pre>
      {info && <p className="field__hint logs__path">{info.path}</p>}
    </section>
  );
}
