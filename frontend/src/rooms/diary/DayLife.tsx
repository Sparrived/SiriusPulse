import { motion } from 'motion/react';
import { localDay, useAutonomy, useLife } from '@/lib/queries-journal';
import type { LifeBlock } from '@/lib/types-journal';

const STATUS: Record<string, string> = {
  planned: '还没到',
  doing: '正在做',
  done: '做了',
  skipped: '没做',
  missed: '错过了',
};

const clock = (iso: string) =>
  new Date(iso).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });

function BlockLine({ block, title }: { block: LifeBlock; title?: string }) {
  return (
    <>
      <span className="daylife__time num">
        {block.start}–{block.end}
      </span>
      <div className="daylife__body">
        <p className="hand">
          {block.activity}
          {title && <span className="daylife__project"> · {title}</span>}
        </p>
        <span className={`daylife__status is-${block.status}`}>{STATUS[block.status] ?? block.status}</span>
        {block.status === 'done' && block.outcome && <p className="daylife__outcome">→ {block.outcome}</p>}
        {(block.status === 'skipped' || block.status === 'missed') && block.reason && (
          <p className="daylife__outcome">（{block.reason}）</p>
        )}
      </div>
    </>
  );
}

/** 她的一天：早上给自己排的日程、每一段做了没有、这一天里她主动说给别人的话，以及这天夜里的梦。 */
export function DayLife({ day }: { day: string }) {
  const { data, isLoading } = useLife(day);
  const { data: autonomy } = useAutonomy();
  if (isLoading) return <p className="mirror__loading hand">翻到这一天……</p>;

  const agenda = data?.agenda;
  const titles = new Map((data?.projects ?? []).map((p) => [p.project_id, p.title]));
  const shares = (autonomy?.episodes ?? []).filter(
    (ep) => ep.kind === 'share' && localDay(new Date(ep.ended_at || ep.started_at)) === day,
  );
  const note = data?.self_note;
  const dream = data?.dream;
  const isToday = day === localDay();

  return (
    <div className="daylife">
      <section className="sheet paper">
        {agenda ? (
          <>
            {agenda.note && <p className="daylife__note hand">“{agenda.note}”</p>}
            <ol className="daylife__list">
              {agenda.blocks.map((block, i) => (
                <motion.li
                  key={`${block.start}-${i}`}
                  className={`is-${block.kind} is-${block.status}`}
                  initial={{ opacity: 0, x: -10 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ delay: i * 0.05 }}
                >
                  <span className="daylife__dot" aria-hidden />
                  <BlockLine
                    block={block}
                    title={block.project_id ? titles.get(block.project_id) : undefined}
                  />
                </motion.li>
              ))}
            </ol>
          </>
        ) : (
          <div className="daylife__empty">
            <p className="hand">{isToday ? '今天的日程还没排。' : '这一天没有留下日程。'}</p>
            <p className="field__hint">
              她每天醒来（{data?.now.wake_time ?? '07:30'}
              ）会先给自己排一下今天：哪几段做自己的事，哪几段歇着。
            </p>
          </div>
        )}
      </section>

      {!!shares.length && (
        <section className="sheet paper daylife__shares">
          <h3 className="hand">这天她主动说给别人的话</h3>
          <ul>
            {shares.map((ep) => (
              <li key={ep.episode_id}>
                <span className="daylife__time num">{clock(ep.ended_at || ep.started_at)}</span>
                <p>{ep.outcome}</p>
              </li>
            ))}
          </ul>
        </section>
      )}

      {dream && (
        <section className="sheet paper daylife__dream">
          <h3 className="hand">这天夜里，她梦见</h3>
          <p className="hand">{dream.text}</p>
          {dream.feeling && <p className="field__hint">醒来时{dream.feeling}</p>}
        </section>
      )}

      {isToday && note?.recent && (
        <section className="sheet paper daylife__self">
          <h3 className="hand">她最近</h3>
          <p>{note.recent}</p>
          {!!note.interests.length && (
            <div className="daylife__tags">
              {note.interests.map((tag) => (
                <span key={tag} className="chip">
                  {tag}
                </span>
              ))}
            </div>
          )}
          {note.tomorrow && <p className="field__hint">明天想：{note.tomorrow}</p>}
        </section>
      )}
    </div>
  );
}
