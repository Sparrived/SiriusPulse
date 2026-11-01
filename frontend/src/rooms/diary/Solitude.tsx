import { motion } from 'motion/react';
import { localDay, useAutonomy } from '@/lib/queries-journal';
import { ago } from '@/lib/time';

const KIND: Record<string, string> = {
  musing: '发呆想事情',
  reading: '读点东西',
  helping: '帮别人的忙',
  caring: '惦记某个人',
  writing: '写点东西',
  exploring: '到处看看',
};

const clock = (iso: string) =>
  new Date(iso).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });

/** 独处时光：没人找她的时候，她自己做了些什么 */
export function Solitude({ day }: { day: string }) {
  const { data, isLoading } = useAutonomy();
  const episodes = data?.episodes ?? [];
  if (isLoading) return <p className="mirror__loading hand">翻到独处的那几页……</p>;
  if (!episodes.length) {
    return (
      <section className="sheet paper solitude__empty">
        <p className="hand">还没有独处的记录。</p>
        <p className="field__hint">当没人找她、她又有空的时候，她会自己去读点东西、想想事情，记在这里。</p>
      </section>
    );
  }

  let lastDay = '';
  return (
    <section className="sheet paper solitude">
      <ol>
        {episodes.map((ep, i) => {
          const at = ep.ended_at || ep.started_at;
          const d = localDay(new Date(at));
          const header = d !== lastDay ? d : null;
          lastDay = d;
          return (
            <motion.li
              key={ep.episode_id}
              className={d === day ? 'is-today' : ''}
              initial={{ opacity: 0, x: -10 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ delay: i * 0.04 }}
            >
              {header && (
                <span className="solitude__day hand">{header === localDay() ? '今天' : header}</span>
              )}
              <span className="solitude__dot" aria-hidden />
              <div className="solitude__body">
                <span className="solitude__kind">
                  {KIND[ep.kind] ?? ep.kind} · {clock(ep.started_at)}
                  {ep.ended_at && ` – ${clock(ep.ended_at)}`} · {ago(at)}
                </span>
                <p className="hand">{ep.seed || '（没留下题目）'}</p>
                {ep.outcome && <p className="solitude__outcome">→ {ep.outcome}</p>}
              </div>
            </motion.li>
          );
        })}
      </ol>
    </section>
  );
}
