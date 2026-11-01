import { AnimatePresence, motion } from 'motion/react';
import { useEffect, useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import { MEMORY_KINDS, memoryColor, memoryLabel } from '@/lib/memory-kinds';
import { useCurrentPersona } from '@/lib/queries';
import { useMemories } from '@/lib/queries-journal';
import type { MemoryUnit } from '@/lib/types-journal';
import { ago } from '@/lib/time';
import { useScene } from '@/state/scene';
import { blankMemory, MemoryEditor } from './MemoryEditor';
import { Tidy } from './Tidy';
import './memories.css';

const SORTS = { salience: '最重要的', recent: '最近的' } as const;

/** 回忆：一只装满萤火的玻璃瓶。每只萤火是她记得的一件事。 */
export function Memories() {
  const persona = useCurrentPersona();
  const { data, isLoading, isError, error } = useMemories();
  const focus = useScene((s) => s.focusMemory);
  const hover = useScene((s) => s.hoverMemory);
  const fromJar = useScene((s) => s.hoverFromJar);
  const set = useScene((s) => s.set);
  const [text, setText] = useState('');
  const [group, setGroup] = useState('');
  const [kinds, setKinds] = useState<string[]>([]);
  const [sort, setSort] = useState<keyof typeof SORTS>('salience');
  const [creating, setCreating] = useState<MemoryUnit | null>(null);
  const [tidy, setTidy] = useState(false);

  const units = useMemo(() => data?.units ?? [], [data]);
  const matches = useMemo(() => {
    const q = text.trim().toLowerCase();
    return (u: MemoryUnit) =>
      (!group || u.group_id === group) &&
      (!kinds.length || kinds.includes(u.unit_type)) &&
      (!q ||
        [u.summary, ...u.participants, ...u.topics, ...u.keywords, u.scope_id]
          .join(' ')
          .toLowerCase()
          .includes(q));
  }, [text, group, kinds]);

  const shown = useMemo(() => {
    const list = units.filter(matches);
    return sort === 'salience'
      ? list.sort((a, b) => b.salience - a.salience)
      : list.sort((a, b) => (b.created_at > a.created_at ? 1 : -1));
  }, [units, matches, sort]);

  // 把记忆交给 3D 瓶子；离开房间时清空，别让看不见的萤火挡住别的房间
  useEffect(() => {
    set({
      fireflies: units.map((u) => ({
        id: u.unit_id,
        type: u.unit_type,
        salience: u.salience,
        confidence: u.confidence,
        dim: !matches(u),
      })),
    });
  }, [units, matches, set]);
  useEffect(() => () => set({ fireflies: [], focusMemory: null, hoverMemory: null }), [set]);

  // 在瓶子里点中萤火：列表滚到那一条
  useEffect(() => {
    if (focus)
      document.getElementById(`memory-${focus}`)?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }, [focus]);

  if (!persona) return null;
  const hovered = hover ? units.find((u) => u.unit_id === hover) : null;
  const counts = new Map<string, number>();
  units.forEach((u) => counts.set(u.unit_type, (counts.get(u.unit_type) ?? 0) + 1));

  return (
    <motion.div
      className="room-column room-column--wide memories"
      initial={{ opacity: 0, x: 40 }}
      animate={{ opacity: 1, x: 0 }}
      exit={{ opacity: 0, x: 30 }}
      transition={{ type: 'spring', stiffness: 120, damping: 20 }}
    >
      <div className="room-title">
        <div>
          <h1 className="hand">回忆</h1>
          <p>
            {persona.persona_name || persona.name}记得 <b className="num">{units.length}</b>{' '}
            件事。瓶子里每只萤火都是一件：越大越重要，越亮越确定。
          </p>
        </div>
      </div>

      <div className="memories__tools paper">
        <input
          className="input"
          placeholder="找一件事、一个人、一个词……"
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <select className="input memories__group" value={group} onChange={(e) => setGroup(e.target.value)}>
          <option value="">所有群</option>
          {(data?.groups ?? []).map((g) => (
            <option key={g} value={g}>
              群 {g}
            </option>
          ))}
        </select>
        <div className="memories__kinds">
          {MEMORY_KINDS.filter((k) => counts.has(k.value)).map((k) => (
            <button
              key={k.value}
              className={`kind-chip${kinds.includes(k.value) ? ' is-on' : ''}`}
              style={{ '--kind': k.color } as React.CSSProperties}
              onClick={() =>
                setKinds(kinds.includes(k.value) ? kinds.filter((x) => x !== k.value) : [...kinds, k.value])
              }
            >
              {k.label}
              <em className="num">{counts.get(k.value)}</em>
            </button>
          ))}
        </div>
        <div className="memories__actions">
          <span className="memories__sort">
            {Object.entries(SORTS).map(([k, label]) => (
              <button
                key={k}
                className={sort === k ? 'is-on' : ''}
                onClick={() => setSort(k as keyof typeof SORTS)}
              >
                {label}
              </button>
            ))}
          </span>
          <button className="soft-btn ghost" onClick={() => setTidy((v) => !v)}>
            整理回忆
          </button>
          <button
            className="soft-btn"
            onClick={() => {
              set({ focusMemory: null });
              setCreating(blankMemory(group || data?.groups[0] || ''));
            }}
          >
            ＋ 放一件新的进去
          </button>
        </div>
      </div>

      <AnimatePresence>
        {tidy && <Tidy persona={persona.name} awake={persona.running} onClose={() => setTidy(false)} />}
      </AnimatePresence>

      <AnimatePresence>
        {creating && (
          <section className="sheet paper">
            <header className="sheet__head">
              <h2 className="hand">一件新的回忆</h2>
            </header>
            <MemoryEditor
              persona={persona.name}
              unit={creating}
              groups={data?.groups ?? []}
              onDone={() => setCreating(null)}
            />
          </section>
        )}
      </AnimatePresence>

      {isError && <p className="mirror__error">{(error as Error).message}</p>}
      {isLoading && <p className="mirror__loading hand">打开瓶子……</p>}
      {!isLoading && !units.length && (
        <section className="sheet paper">
          <p className="hand">瓶子还是空的。</p>
          <p className="field__hint">聊得多了，她会自己把值得记住的事收进来；你也可以亲手放一件进去。</p>
        </section>
      )}

      <ul className="memory-list">
        {shown.map((u) => {
          const open = focus === u.unit_id;
          return (
            <li
              key={u.unit_id}
              id={`memory-${u.unit_id}`}
              className={`memory paper${open ? ' is-open' : ''}${hover === u.unit_id ? ' is-hover' : ''}`}
            >
              <button
                className="memory__head"
                onClick={() => set({ focusMemory: open ? null : u.unit_id })}
                onPointerEnter={() => set({ hoverMemory: u.unit_id, hoverFromJar: false })}
                onPointerLeave={() => set({ hoverMemory: null })}
              >
                <i
                  className="memory__dot"
                  style={{ background: memoryColor(u.unit_type), opacity: 0.4 + u.confidence * 0.6 }}
                />
                <span className="memory__summary">{u.summary}</span>
                <span className="memory__meta">
                  {memoryLabel(u.unit_type)}
                  {u.participants.length > 0 && ` · ${u.participants.join('、')}`} · {ago(u.created_at)}
                  {!u.should_prompt && ' · 不会主动想起'}
                </span>
              </button>
              <AnimatePresence>
                {open && (
                  <MemoryEditor
                    key={u.unit_id}
                    persona={persona.name}
                    unit={u}
                    groups={data?.groups ?? []}
                    onDone={() => set({ focusMemory: null })}
                  />
                )}
              </AnimatePresence>
            </li>
          );
        })}
      </ul>

      {hovered &&
        fromJar &&
        createPortal(
          <div className="firefly-tip paper" role="tooltip">
            <i style={{ background: memoryColor(hovered.unit_type) }} />
            <span>{hovered.summary}</span>
          </div>,
          document.body,
        )}
    </motion.div>
  );
}
