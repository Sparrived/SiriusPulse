import { AnimatePresence, motion } from 'motion/react';
import { useEffect, useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import { DIARY_PAGE, useDiaryDay } from '@/lib/queries-journal';
import type { PersonaSummary } from '@/lib/types';
import type { ChatEntry } from '@/lib/types-journal';
import { InnerPage } from './InnerPage';

const clock = (iso: string) =>
  new Date(iso).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
const shortGroup = (id: string) => (id.length > 6 ? `…${id.slice(-4)}` : id);

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = window.setTimeout(() => setV(value), ms);
    return () => window.clearTimeout(t);
  }, [value, ms]);
  return v;
}

/** 这一天的聊天：像翻一本手账，她说的话靠右，别人说的靠左；点一句话翻开内页 */
export function ChatLog({
  persona,
  day,
  onJump,
  jumpLabel,
}: {
  persona: PersonaSummary;
  day: string;
  onJump?: () => void;
  jumpLabel?: string;
}) {
  const [group, setGroup] = useState('');
  const [text, setText] = useState('');
  const search = useDebounced(text.trim().toLowerCase(), 400);
  const [page, setPage] = useState(0);
  const [open, setOpen] = useState<ChatEntry | null>(null);
  const { data, isFetching, isError, error } = useDiaryDay(day, { group, search }, page);
  const name = persona.persona_name || persona.name;

  useEffect(() => setPage(0), [day, group, search]);

  // 接口按新到旧给，日记要从早读到晚
  const entries = useMemo(() => [...(data?.messages ?? [])].reverse(), [data]);
  const total = data?.total ?? 0;
  const groups = data?.groups ?? [];
  const hasOlder = (page + 1) * DIARY_PAGE < total && (page + 1) * DIARY_PAGE <= 1000;

  let lastGroup = '';
  return (
    <>
      <div className="chatlog__filters">
        <select className="input chatlog__group" value={group} onChange={(e) => setGroup(e.target.value)}>
          <option value="">所有群和私聊</option>
          {groups.map((g) => (
            <option key={g} value={g}>
              群 {g}
            </option>
          ))}
        </select>
        <input
          className="input chatlog__search"
          placeholder="找一句话……"
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <span className="chatlog__count">{isFetching ? '翻找中……' : `这一天 ${total} 句`}</span>
      </div>

      {isError && <p className="mirror__error">{(error as Error).message}</p>}

      <section className="chatlog paper">
        {hasOlder && (
          <button className="chatlog__more" onClick={() => setPage(page + 1)}>
            ↑ 更早的
          </button>
        )}
        {!isFetching && !entries.length && (
          <div className="chatlog__empty">
            <p className="hand">{search ? '这一天没有说过这句话' : '这一天很安静，没有人来聊天。'}</p>
            {!search && onJump && (
              <button className="soft-btn ghost" onClick={onJump}>
                翻到上一次有聊天的那天（{jumpLabel}）
              </button>
            )}
          </div>
        )}
        {entries.map((entry, i) => {
          const her = entry.role === 'assistant';
          const side = entry.role === 'system' || entry.role === 'tool' ? 'note' : her ? 'her' : 'them';
          const showGroup = !group && entry.group_id !== lastGroup;
          lastGroup = entry.group_id;
          return (
            <div key={entry.entry_id ?? `${entry.timestamp}-${i}`}>
              {showGroup && <div className="chatlog__divider">群 {shortGroup(entry.group_id)}</div>}
              <motion.button
                className={`line line--${side}`}
                onClick={() => setOpen(entry)}
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: Math.min(i, 20) * 0.012 }}
              >
                <span className="line__meta">
                  {her ? name : entry.speaker_name || entry.user_id || (side === 'note' ? '系统' : '有人')}
                  <em>{clock(entry.timestamp)}</em>
                  {(entry.reasoning_content || entry.tool_calls?.length) && <i title="有内页">✎</i>}
                  {entry.memory_compressed && <i title="已整理进回忆">◌</i>}
                </span>
                <span className="line__text">
                  {entry.content || (entry.tool_calls?.length ? '（用了小工具）' : '（空）')}
                </span>
              </motion.button>
            </div>
          );
        })}
        {page > 0 && (
          <button className="chatlog__more" onClick={() => setPage(page - 1)}>
            ↓ 更晚的
          </button>
        )}
      </section>

      {/* 房间那一列带着位移动画，fixed 定位会被它困住：内页挂到 body 上 */}
      {createPortal(
        <AnimatePresence>
          {open && <InnerPage entry={open} persona={persona} onClose={() => setOpen(null)} />}
        </AnimatePresence>,
        document.body,
      )}
    </>
  );
}
