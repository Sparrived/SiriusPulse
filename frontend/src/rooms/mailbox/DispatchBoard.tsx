import { motion } from 'motion/react';
import { useDispatch, usePersonas } from '@/lib/queries';
import { ago } from '@/lib/time';
import { Sheet } from '@/ui/form/fields';

const STATUS: Record<string, string> = {
  sent: '开口说了',
  silent: '想了想没说',
  granted: '拿到话筒',
  expired: '话筒过期了',
  observed: '在旁边听着',
  denied: '让给别人了',
  rejected: '让给别人了',
  pending: '在等',
};

const STRATEGY: Record<string, string> = {
  immediate: '马上回',
  delayed: '想一想再回',
  silent: '不回',
  proactive: '主动说',
};

const secondsAgo = (t?: number) => (t ? ago(new Date(t * 1000).toISOString()) : '');

/** 群聊排班：几位伙伴同在一个群时，话筒在谁手里、最近谁说了什么 */
export function DispatchBoard() {
  const { data } = useDispatch();
  const { data: personas } = usePersonas();
  const nameOf = (id?: string) => {
    if (!id) return '有人';
    const p = personas?.personas.find((x) => x.name === id || id.includes(x.name));
    return p ? p.persona_name || p.name : id;
  };

  if (!data?.available) {
    return (
      <Sheet title="群聊排班" fold>
        <p className="field__hint">
          还没有排班记录。只有当几位伙伴同在一个群、并且都参与排班时，这里才会热闹起来。
        </p>
      </Sheet>
    );
  }

  const s = data.summary;
  return (
    <Sheet title="群聊排班" aside={<span className="field__hint">每 5 秒刷新 · 所有伙伴共用</span>}>
      <div className="board__summary">
        <span>
          <b className="num">{s?.workers_online ?? 0}</b>/{s?.workers_total ?? 0} 位在线
        </span>
        <span>
          <b className="num">{s?.active_turns ?? 0}</b> 个群正在说话
        </span>
        <span>
          这一天排了 <b className="num">{s?.decisions_24h ?? 0}</b> 次、开口{' '}
          <b className="num">{s?.sent_24h ?? 0}</b> 次
        </span>
      </div>

      {!!data.groups?.length && (
        <div className="board__groups">
          {data.groups.slice(0, 8).map((g) => (
            <div key={g.group_id} className={`board__group${g.active ? ' is-live' : ''}`}>
              <span className="num board__gid">群 {g.group_id}</span>
              {g.active ? (
                <span className="board__mic hand">
                  <motion.span
                    className="board__dot"
                    animate={{ scale: [1, 1.4, 1] }}
                    transition={{ repeat: Infinity, duration: 1.4 }}
                  />
                  {nameOf(g.active_worker_id)} 拿着话筒
                  {g.active_remaining_seconds ? ` · 还剩 ${Math.ceil(g.active_remaining_seconds)} 秒` : ''}
                </span>
              ) : (
                <span className="board__quiet">安静 · {secondsAgo(g.last_reply_at) || '还没人说过话'}</span>
              )}
              {!!g.peer_turns && <span className="board__turns">伙伴接了 {g.peer_turns} 轮</span>}
            </div>
          ))}
        </div>
      )}

      {!!data.workers?.length && (
        <div className="board__workers">
          {data.workers.map((w) => (
            <span
              key={w.worker_id}
              className={`board__worker${w.online ? ' is-on' : ''}`}
              title={w.worker_id}
            >
              <span className="board__wdot" />
              {nameOf(w.worker_id)}
              <em className="num">{w.recent_reply_count ?? 0}</em>
            </span>
          ))}
        </div>
      )}

      {!!data.events?.length && (
        <ul className="board__events">
          {data.events.slice(0, 10).map((e) => (
            <li key={e.event_id}>
              <span className="board__who hand">{nameOf(e.worker_id)}</span>
              <span>
                {STATUS[e.status] ?? e.status}
                {e.response_strategy && ` · ${STRATEGY[e.response_strategy] ?? e.response_strategy}`}
                {typeof e.final_score === 'number' && <em className="num"> {e.final_score.toFixed(2)}</em>}
              </span>
              <span className="board__where">
                群 …{e.group_id.slice(-4)} · {secondsAgo(e.updated_at)}
              </span>
              {e.reason && <span className="board__reason">{e.reason}</span>}
            </li>
          ))}
        </ul>
      )}
    </Sheet>
  );
}
