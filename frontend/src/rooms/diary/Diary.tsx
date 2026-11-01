import { AnimatePresence, motion } from 'motion/react';
import { useEffect, useMemo, useState } from 'react';
import { localDay, useDayTimeline } from '@/lib/queries-journal';
import { useCurrentPersona } from '@/lib/queries';
import { Segmented } from '@/ui/form/fields';
import { Calendar } from './Calendar';
import { ChatLog } from './ChatLog';
import { DayLife } from './DayLife';
import { WorkLog } from './WorkLog';
import { Works } from './Works';
import './diary.css';

type Tab = 'chat' | 'day' | 'works' | 'work';

const WEEK = ['星期日', '星期一', '星期二', '星期三', '星期四', '星期五', '星期六'];

function shift(day: string, delta: number) {
  const [y, m, d] = day.split('-').map(Number);
  return localDay(new Date(y, m - 1, d + delta));
}

function dayTitle(day: string) {
  const [y, m, d] = day.split('-').map(Number);
  const date = new Date(y, m - 1, d);
  const today = localDay();
  const rel = day === today ? ' · 今天' : day === shift(today, -1) ? ' · 昨天' : '';
  const year = y !== new Date().getFullYear() ? `${y}年` : '';
  return `${year}${m}月${d}日 ${WEEK[date.getDay()]}${rel}`;
}

/** 日记：她的一天。聊过什么、给自己排了什么、写下了什么、认真工作时怎么一步步做完的。 */
export function Diary() {
  const persona = useCurrentPersona();
  const [tab, setTab] = useState<Tab>('chat');
  const [day, setDay] = useState(localDay());
  const [calendar, setCalendar] = useState(false);
  const { data: timeline } = useDayTimeline();

  // 有动静的日子（存档按 UTC 日期分桶，换算成本地日期只是近似，足够在日历上点亮）
  const activeDays = useMemo(() => {
    const out = new Map<string, number>();
    for (const [utcDay, groups] of Object.entries(timeline?.basic_timeline.buckets ?? {})) {
      const n = Object.values(groups).reduce((sum, g) => sum + (g.human ?? 0) + (g.assistant ?? 0), 0);
      out.set(utcDay, (out.get(utcDay) ?? 0) + n);
    }
    return out;
  }, [timeline]);

  // 换了伙伴就回到今天
  useEffect(() => setDay(localDay()), [persona?.name]);

  if (!persona) return null;
  const latestBefore = [...activeDays.keys()]
    .sort()
    .reverse()
    .find((d) => d <= day && d !== day);

  return (
    <motion.div
      className="room-column room-column--wide diary"
      initial={{ opacity: 0, y: 30, rotate: 0.5 }}
      animate={{ opacity: 1, y: 0, rotate: 0 }}
      exit={{ opacity: 0, y: 20 }}
      transition={{ type: 'spring', stiffness: 120, damping: 20 }}
    >
      <div className="room-title">
        <div>
          <h1 className="hand">日记</h1>
          <p>{persona.persona_name || persona.name}的一天：聊过的话、自己的日程和作品、认真做完的事。</p>
        </div>
      </div>

      <div className="diary__bar paper">
        <div className="diary__date">
          <button className="diary__arrow" onClick={() => setDay(shift(day, -1))} aria-label="前一天">
            ‹
          </button>
          <button className="diary__day hand" onClick={() => setCalendar((v) => !v)} aria-expanded={calendar}>
            {dayTitle(day)}
          </button>
          <button
            className="diary__arrow"
            onClick={() => setDay(shift(day, 1))}
            disabled={day >= localDay()}
            aria-label="后一天"
          >
            ›
          </button>
          {day !== localDay() && (
            <button className="diary__today" onClick={() => setDay(localDay())}>
              回到今天
            </button>
          )}
        </div>
        <Segmented
          value={tab}
          onChange={setTab}
          options={[
            { value: 'chat', label: '聊天' },
            { value: 'day', label: '她的一天' },
            { value: 'works', label: '她的作品' },
            { value: 'work', label: '认真工作' },
          ]}
        />
        <AnimatePresence>
          {calendar && (
            <Calendar
              day={day}
              activeDays={activeDays}
              onPick={(d) => {
                setDay(d);
                setCalendar(false);
              }}
              onClose={() => setCalendar(false)}
            />
          )}
        </AnimatePresence>
      </div>

      <AnimatePresence mode="wait">
        <motion.div
          key={tab}
          className="diary__pane"
          initial={{ opacity: 0, x: 18 }}
          animate={{ opacity: 1, x: 0 }}
          exit={{ opacity: 0, x: -14 }}
          transition={{ duration: 0.25 }}
        >
          {tab === 'chat' && (
            <ChatLog
              persona={persona}
              day={day}
              onJump={latestBefore ? () => setDay(latestBefore) : undefined}
              jumpLabel={latestBefore ? dayTitle(latestBefore) : undefined}
            />
          )}
          {tab === 'day' && <DayLife day={day} />}
          {tab === 'works' && <Works />}
          {tab === 'work' && <WorkLog persona={persona.name} />}
        </motion.div>
      </AnimatePresence>
    </motion.div>
  );
}
