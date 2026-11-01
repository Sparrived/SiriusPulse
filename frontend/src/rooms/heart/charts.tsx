import { motion } from 'motion/react';
import { useState } from 'react';
import { EMOTION_COLOR, EMOTION_SPOTS, type Sky } from '@/lib/feelings';
import { EMOTION_CN } from '@/lib/mood';
import type { CognitionEvent, Histogram } from '@/lib/types-journal';

/** 会动的天气小画：太阳慢慢转，云飘，雨一滴滴落下 */
export function WeatherGlyph({ sky, size = 92 }: { sky: Sky; size?: number }) {
  const sun = sky === 'sun' || sky === 'partly';
  const cloud = sky !== 'sun';
  const rain = sky === 'drizzle' || sky === 'rain';
  return (
    <svg width={size} height={size} viewBox="0 0 100 100" className={`glyph glyph--${sky}`} aria-hidden>
      {sun && (
        <g className="glyph__sun" style={{ transformOrigin: sky === 'sun' ? '50px 50px' : '38px 38px' }}>
          <circle
            cx={sky === 'sun' ? 50 : 38}
            cy={sky === 'sun' ? 50 : 38}
            r={sky === 'sun' ? 18 : 14}
            fill="#ffd36e"
          />
          {Array.from({ length: 8 }, (_, i) => {
            const a = (i / 8) * Math.PI * 2;
            const cx = sky === 'sun' ? 50 : 38;
            const r1 = sky === 'sun' ? 26 : 20;
            const r2 = sky === 'sun' ? 34 : 26;
            return (
              <line
                key={i}
                x1={cx + Math.cos(a) * r1}
                y1={cx + Math.sin(a) * r1}
                x2={cx + Math.cos(a) * r2}
                y2={cx + Math.sin(a) * r2}
                stroke="#ffc54a"
                strokeWidth="4"
                strokeLinecap="round"
              />
            );
          })}
        </g>
      )}
      {cloud && (
        <g className="glyph__cloud">
          <path
            d="M30 72a14 14 0 0 1 1-28 19 19 0 0 1 36 4 12 12 0 0 1 2 24Z"
            fill={rain ? '#c3cbe6' : '#f4f6ff'}
            stroke="#aab6e0"
            strokeWidth="2.5"
          />
        </g>
      )}
      {rain &&
        Array.from({ length: sky === 'rain' ? 5 : 3 }, (_, i) => (
          <line
            key={i}
            className="glyph__drop"
            style={{ animationDelay: `${i * 0.27}s` }}
            x1={36 + i * 8}
            y1={78}
            x2={33 + i * 8}
            y2={86}
            stroke="#7d9be6"
            strokeWidth="3"
            strokeLinecap="round"
          />
        ))}
    </svg>
  );
}

/**
 * 情绪的原野：横轴是开不开心，纵轴是激不激动。
 * 最近每一句话都是一颗小点，落在对方当时的心情上。
 */
export function EmotionField({ events }: { events: CognitionEvent[] }) {
  const W = 560;
  const H = 300;
  const pad = 26;
  const x = (v: number) => pad + ((v + 1) / 2) * (W - pad * 2);
  const y = (a: number) => H - pad - a * (H - pad * 2);
  const [hover, setHover] = useState<CognitionEvent | null>(null);
  const present = new Set(events.map((e) => e.basic_emotion));

  return (
    <div className="field-wrap">
      <svg viewBox={`0 0 ${W} ${H}`} className="emotion-field">
        <defs>
          <radialGradient id="ef-warm" cx="85%" cy="15%" r="70%">
            <stop offset="0%" stopColor="#ffd7a8" stopOpacity="0.7" />
            <stop offset="100%" stopColor="#ffd7a8" stopOpacity="0" />
          </radialGradient>
          <radialGradient id="ef-calm" cx="85%" cy="90%" r="60%">
            <stop offset="0%" stopColor="#bfe8d0" stopOpacity="0.7" />
            <stop offset="100%" stopColor="#bfe8d0" stopOpacity="0" />
          </radialGradient>
          <radialGradient id="ef-storm" cx="10%" cy="12%" r="60%">
            <stop offset="0%" stopColor="#d9b8e8" stopOpacity="0.6" />
            <stop offset="100%" stopColor="#d9b8e8" stopOpacity="0" />
          </radialGradient>
          <radialGradient id="ef-blue" cx="10%" cy="90%" r="60%">
            <stop offset="0%" stopColor="#b8c6ee" stopOpacity="0.7" />
            <stop offset="100%" stopColor="#b8c6ee" stopOpacity="0" />
          </radialGradient>
        </defs>
        <rect x="0" y="0" width={W} height={H} rx="18" fill="#fbfbff" />
        {['ef-warm', 'ef-calm', 'ef-storm', 'ef-blue'].map((id) => (
          <rect key={id} x="0" y="0" width={W} height={H} rx="18" fill={`url(#${id})`} />
        ))}
        <line x1={x(0)} x2={x(0)} y1={pad} y2={H - pad} stroke="#d4daf2" strokeDasharray="3 5" />
        <line x1={pad} x2={W - pad} y1={y(0.5)} y2={y(0.5)} stroke="#d4daf2" strokeDasharray="3 5" />
        <text x={pad + 4} y={H - 8} className="field-axis">
          ← 不开心
        </text>
        <text x={W - pad - 4} y={H - 8} textAnchor="end" className="field-axis">
          开心 →
        </text>
        <text x={x(0) + 6} y={pad - 8} className="field-axis">
          激动
        </text>
        <text x={x(0) + 6} y={H - 8} className="field-axis">
          平静
        </text>
        {events.map((e, i) => (
          <motion.circle
            key={e.id}
            cx={x(e.valence)}
            cy={y(e.arousal)}
            r={3 + e.intensity * 5}
            fill={EMOTION_COLOR[e.basic_emotion] ?? '#c8cde3'}
            fillOpacity={0.75}
            stroke="#fff"
            strokeWidth={1}
            initial={{ scale: 0, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            transition={{ delay: Math.min(i, 120) * 0.006, type: 'spring', stiffness: 300, damping: 20 }}
            onPointerEnter={() => setHover(e)}
            onPointerLeave={() => setHover(null)}
          />
        ))}
        {/* 名字画在点的上面，别被盖住 */}
        {Object.entries(EMOTION_SPOTS)
          .filter(([k]) => present.has(k) && k !== 'NEUTRAL')
          .map(([k, [v, a]]) => (
            <text key={k} x={x(v)} y={y(a) - 12} textAnchor="middle" className="field-label">
              {EMOTION_CN[k]}
            </text>
          ))}
      </svg>
      <p className="field-hover">
        {hover
          ? `${new Date(hover.timestamp * 1000).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })} · 群 …${hover.group_id.slice(-4)} · 对方有点${EMOTION_CN[hover.basic_emotion] ?? hover.basic_emotion}`
          : '把鼠标放到小点上，看看是哪一句'}
      </p>
    </div>
  );
}

/** 24 小时的小钟：每个小时一片花瓣，越长越热闹 */
export function DayClock({ hours }: { hours: Record<string, number> }) {
  const values = Array.from({ length: 24 }, (_, h) => hours[String(h)] ?? 0);
  const max = Math.max(1, ...values);
  const peak = values.indexOf(Math.max(...values));
  const now = new Date().getHours();
  return (
    <div className="dayclock">
      <svg viewBox="-110 -110 220 220" width="220" height="220" aria-label="一天里的热闹程度">
        <circle r="34" fill="#fff" stroke="#e3e8fa" />
        {values.map((v, h) => {
          const a = (h / 24) * Math.PI * 2 - Math.PI / 2;
          const len = 8 + (v / max) * 58;
          const night = h < 6 || h >= 22;
          return (
            <motion.line
              key={h}
              x1={Math.cos(a) * 40}
              y1={Math.sin(a) * 40}
              x2={Math.cos(a) * (40 + len)}
              y2={Math.sin(a) * (40 + len)}
              stroke={h === now ? '#f78aa8' : night ? '#8f9fe0' : '#ffc46e'}
              strokeWidth={h === peak ? 9 : 7}
              strokeLinecap="round"
              initial={{ pathLength: 0 }}
              animate={{ pathLength: 1 }}
              transition={{ delay: h * 0.02, duration: 0.6 }}
            />
          );
        })}
        {[0, 6, 12, 18].map((h) => {
          const a = (h / 24) * Math.PI * 2 - Math.PI / 2;
          return (
            <text
              key={h}
              x={Math.cos(a) * 22}
              y={Math.sin(a) * 22 + 4}
              textAnchor="middle"
              className="clock-num"
            >
              {h}
            </text>
          );
        })}
      </svg>
      <p className="hand">
        {values.some(Boolean) ? `${peak} 点前后最热闹` : '还没有记录'}
        {values[now] ? `，现在（${now} 点）也挺有人气` : ''}
      </p>
    </div>
  );
}

/** 横向的一排柔和条形 */
export function SoftBars({
  items,
  color = () => '#a9bdf5',
}: {
  items: { label: string; value: number; key: string }[];
  color?: (key: string) => string;
}) {
  const max = Math.max(1, ...items.map((i) => i.value));
  const total = items.reduce((s, i) => s + i.value, 0) || 1;
  return (
    <ul className="softbars">
      {items.map((it, i) => (
        <li key={it.key}>
          <span className="softbars__label">{it.label}</span>
          <span className="softbars__track">
            <motion.i
              style={{ background: color(it.key) }}
              initial={{ width: 0 }}
              animate={{ width: `${(it.value / max) * 100}%` }}
              transition={{ delay: i * 0.05, duration: 0.6, ease: [0.22, 1, 0.36, 1] }}
            />
          </span>
          <em className="num">{Math.round((it.value / total) * 100)}%</em>
        </li>
      ))}
    </ul>
  );
}

export function MiniHistogram({ h, color }: { h?: Histogram; color: string }) {
  if (!h?.counts?.length) return <span className="field__hint">还没有</span>;
  const max = Math.max(1, ...h.counts);
  return (
    <span className="minihist">
      {h.counts.map((c, i) => (
        <i
          key={i}
          style={{
            height: `${8 + (c / max) * 92}%`,
            background: color,
            opacity: 0.35 + (i / h.counts.length) * 0.65,
          }}
        />
      ))}
    </span>
  );
}
