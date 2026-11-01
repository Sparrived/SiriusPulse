import { useMutation } from '@tanstack/react-query';
import { motion } from 'motion/react';
import { useState, type ReactNode } from 'react';
import { deleteChatEntry } from '@/lib/queries-journal';
import { queryClient } from '@/lib/queries';
import type { PersonaSummary } from '@/lib/types';
import type { ChatEntry, ToolCall } from '@/lib/types-journal';

const ROLE: Record<string, string> = {
  system: '系统',
  user: '对方',
  human: '对方',
  assistant: '她',
  tool: '小工具',
};

function pretty(raw?: string) {
  if (!raw) return '';
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

function toolName(t: ToolCall) {
  return t.function?.name ?? t.name ?? '小工具';
}

function Fold({ title, children, open = false }: { title: string; children: ReactNode; open?: boolean }) {
  return (
    <details className="inner__fold" open={open}>
      <summary className="hand">{title}</summary>
      {children}
    </details>
  );
}

/** 翻开内页：这句话背后的东西——她怎么想的、用了什么、想起了什么、当时完整的来回 */
export function InnerPage({
  entry,
  persona,
  onClose,
}: {
  entry: ChatEntry;
  persona: PersonaSummary;
  onClose: () => void;
}) {
  const [confirm, setConfirm] = useState(false);
  const tear = useMutation({
    mutationFn: () => deleteChatEntry(persona.name, entry),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['diary'] });
      onClose();
    },
  });
  const her = entry.role === 'assistant';
  const scores = Object.entries(entry.intent_scores ?? {}).filter(([, v]) => typeof v === 'number');

  return (
    <motion.div
      className="inner-veil"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      onClick={onClose}
    >
      <motion.aside
        className="inner paper"
        role="dialog"
        aria-label="内页"
        onClick={(e) => e.stopPropagation()}
        initial={{ x: 60, opacity: 0, rotate: 1.5 }}
        animate={{ x: 0, opacity: 1, rotate: 0 }}
        exit={{ x: 40, opacity: 0 }}
        transition={{ type: 'spring', stiffness: 200, damping: 24 }}
      >
        <button className="star-card__close" onClick={onClose} aria-label="合上">
          ×
        </button>
        <p className="inner__meta">
          {her
            ? persona.persona_name || persona.name
            : entry.speaker_name || entry.user_id || ROLE[entry.role]}{' '}
          · {new Date(entry.timestamp).toLocaleString('zh-CN')} · 群 {entry.group_id}
        </p>
        <p className="inner__said hand">{entry.content || '（这一句没有文字）'}</p>

        {entry.reasoning_content && (
          <Fold title="她当时在想" open>
            <p className="inner__thought">{entry.reasoning_content}</p>
          </Fold>
        )}

        {!!entry.tool_calls?.length && (
          <Fold title={`用到的小工具（${entry.tool_calls.length}）`} open>
            {entry.tool_calls.map((t, i) => (
              <div key={t.id ?? i} className="inner__tool">
                <b>{toolName(t)}</b>
                <pre>{pretty(t.function?.arguments ?? t.arguments)}</pre>
              </div>
            ))}
          </Fold>
        )}

        {scores.length > 0 && (
          <Fold title="她对这句话的感觉">
            <div className="inner__scores">
              {scores.map(([k, v]) => (
                <span key={k}>
                  {k}
                  <i style={{ width: `${Math.min(100, Math.max(0, v * (v <= 1 ? 100 : 1)))}%` }} />
                  <em className="num">{v.toFixed(2)}</em>
                </span>
              ))}
            </div>
          </Fold>
        )}

        {!!entry.memory_refs?.length && (
          <Fold title={`那时想起的回忆（${entry.memory_refs.length}）`}>
            <ul className="inner__refs">
              {entry.memory_refs.map((r) => (
                <li key={r}>
                  <code>{r}</code>
                </li>
              ))}
            </ul>
          </Fold>
        )}

        {!!entry.conversation_chain?.length && (
          <Fold title={`完整的来回（${entry.conversation_chain.length} 条）`}>
            <ol className="inner__chain">
              {entry.conversation_chain.map((m, i) => (
                <li key={i} className={`inner__chain--${m.role}`}>
                  <b>{ROLE[m.role] ?? m.role}</b>
                  {m.reasoning_content && <p className="inner__thought">{m.reasoning_content}</p>}
                  {m.content && <p>{m.content}</p>}
                  {m.tool_calls?.map((t, j) => (
                    <pre key={j}>
                      {toolName(t)} {pretty(t.function?.arguments ?? t.arguments)}
                    </pre>
                  ))}
                </li>
              ))}
            </ol>
          </Fold>
        )}

        {entry.system_prompt && (
          <Fold title="当时给她的系统提示">
            <pre className="inner__prompt">{entry.system_prompt}</pre>
          </Fold>
        )}

        <div className="inner__tear">
          {persona.running ? (
            <span className="field__hint">她醒着的时候不能改日记；要撕掉这一页，先哄她睡觉。</span>
          ) : confirm ? (
            <>
              <span>撕掉后她就不记得这句话了。</span>
              <button className="soft-btn ghost" onClick={() => setConfirm(false)}>
                算了
              </button>
              <button className="soft-btn blush" disabled={tear.isPending} onClick={() => tear.mutate()}>
                撕掉
              </button>
            </>
          ) : (
            <button className="star-card__farewell" onClick={() => setConfirm(true)}>
              从日记里撕掉这一页…
            </button>
          )}
          {tear.isError && <p className="star-card__error">{(tear.error as Error).message}</p>}
        </div>
      </motion.aside>
    </motion.div>
  );
}
