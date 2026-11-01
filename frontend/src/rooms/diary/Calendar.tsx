import { motion } from 'motion/react';
import { useState } from 'react';
import { localDay } from '@/lib/queries-journal';

/** 小月历：有聊天的日子点一颗小星星，越热闹越亮 */
export function Calendar({
  day,
  activeDays,
  onPick,
  onClose,
}: {
  day: string;
  activeDays: Map<string, number>;
  onPick: (day: string) => void;
  onClose: () => void;
}) {
  const [y0, m0] = day.split('-').map(Number);
  const [view, setView] = useState({ y: y0, m: m0 - 1 });
  const first = new Date(view.y, view.m, 1);
  const days = new Date(view.y, view.m + 1, 0).getDate();
  const today = localDay();
  const max = Math.max(1, ...activeDays.values());
  const cells: (string | null)[] = [
    ...Array.from({ length: first.getDay() }, () => null),
    ...Array.from({ length: days }, (_, i) => localDay(new Date(view.y, view.m, i + 1))),
  ];
  const move = (delta: number) => {
    const d = new Date(view.y, view.m + delta, 1);
    setView({ y: d.getFullYear(), m: d.getMonth() });
  };

  return (
    <motion.div
      className="calendar paper"
      initial={{ opacity: 0, y: -8, scale: 0.97 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, y: -6 }}
      onKeyDown={(e) => e.key === 'Escape' && onClose()}
    >
      <header>
        <button onClick={() => move(-1)} aria-label="上个月">
          ‹
        </button>
        <span className="hand">
          {view.y}年{view.m + 1}月
        </span>
        <button onClick={() => move(1)} aria-label="下个月">
          ›
        </button>
      </header>
      <div className="calendar__grid">
        {['日', '一', '二', '三', '四', '五', '六'].map((w) => (
          <span key={w} className="calendar__week">
            {w}
          </span>
        ))}
        {cells.map((d, i) =>
          d ? (
            <button
              key={d}
              disabled={d > today}
              className={`calendar__cell${d === day ? ' is-on' : ''}${d === today ? ' is-today' : ''}`}
              onClick={() => onPick(d)}
            >
              <span className="num">{Number(d.slice(8))}</span>
              {activeDays.has(d) && (
                <i style={{ opacity: 0.35 + 0.65 * Math.min(1, (activeDays.get(d) ?? 0) / max) }} aria-hidden>
                  ✦
                </i>
              )}
            </button>
          ) : (
            <span key={`pad${i}`} />
          ),
        )}
      </div>
    </motion.div>
  );
}
