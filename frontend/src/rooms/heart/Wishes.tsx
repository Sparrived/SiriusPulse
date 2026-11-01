import { motion } from 'motion/react';
import { useAutonomy } from '@/lib/queries-journal';
import { ago } from '@/lib/time';
import type { Intention } from '@/lib/types-journal';

const STATUS: Record<string, string> = {
  nascent: '刚冒出的念头',
  active: '正惦记着',
  resolved: '已经做到了',
  dropped: '放下了',
};

const KIND: Record<string, string> = {
  musing: '想想事情',
  reading: '想读的',
  helping: '想帮的忙',
  caring: '惦记的人',
  writing: '想写的',
};

const NOTE_COLORS = ['#fff4b8', '#ffe1ea', '#dff3ff', '#e6f7e4', '#efe6ff'];

function hash(s: string) {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return Math.abs(h);
}

function Note({ it, i }: { it: Intention; i: number }) {
  const h = hash(it.intention_id);
  const open = it.status === 'nascent' || it.status === 'active';
  const pin = it.urgency > 0.75 ? '#f0607e' : it.urgency > 0.45 ? '#f0a868' : '#8fb0f0';
  return (
    <motion.li
      className={`note note--${it.status}`}
      style={{ background: NOTE_COLORS[h % NOTE_COLORS.length], rotate: ((h % 7) - 3) * 0.8 }}
      initial={{ opacity: 0, y: -18, rotate: 6 }}
      animate={{ opacity: open ? 1 : 0.62, y: 0, rotate: ((h % 7) - 3) * 0.8 }}
      whileHover={{ y: -4, rotate: 0, scale: 1.02 }}
      transition={{ delay: i * 0.06, type: 'spring', stiffness: 200, damping: 18 }}
    >
      <i
        className="note__pin"
        style={{ background: pin }}
        title={`有多急：${Math.round(it.urgency * 100)}`}
      />
      <span className="note__kind">
        {KIND[it.kind] ?? it.kind}
        {it.resolution === 'tell' ? ' · 想说给' : ''}
        {it.resolution === 'tell' && (it.audience_label || it.audience)}
      </span>
      <p className="note__what hand">{it.what}</p>
      {it.why && <p className="note__why">因为{it.why.replace(/^因为/, '')}</p>}
      <span className="note__foot">
        {STATUS[it.status] ?? it.status} · {ago(it.created_at)}
        {it.attempts > 0 && ` · 试过 ${it.attempts} 次`}
      </span>
      {it.outcome && <p className="note__outcome">→ {it.outcome}</p>}
    </motion.li>
  );
}

/** 心愿便签：她心里惦记着的事，钉在软木板上 */
export function Wishes({ name }: { name: string }) {
  const { data, isLoading } = useAutonomy();
  if (isLoading) return <p className="mirror__loading hand">翻开软木板……</p>;
  const items = data?.intentions ?? [];
  const order = (s: string) => (s === 'active' ? 0 : s === 'nascent' ? 1 : s === 'resolved' ? 2 : 3);
  const sorted = [...items].sort((a, b) => order(a.status) - order(b.status) || b.urgency - a.urgency);

  return (
    <section className="cork paper">
      <header className="sheet__head">
        <h2 className="hand">{name}惦记着的事</h2>
        <span className="field__hint">
          {data?.summary.intentions_open ?? 0} 件还挂在心上 · 一共 {items.length} 张
        </span>
      </header>
      {sorted.length ? (
        <ul className="cork__board">
          {sorted.map((it, i) => (
            <Note key={it.intention_id} it={it} i={i} />
          ))}
        </ul>
      ) : (
        <p className="field__hint">
          她还没有记下什么心愿。聊天里答应别人的事、自己想去做的事，都会变成一张便签钉在这里；图钉越红越着急。
        </p>
      )}
    </section>
  );
}
