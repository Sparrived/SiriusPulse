import { motion } from 'motion/react';
import { useMemo, useState } from 'react';
import { useCurrentPersona } from '@/lib/queries';
import { useTokens, type TokenReport, type UsageRow } from '@/lib/queries-house';
import { ago } from '@/lib/time';
import { CountUp } from '@/ui/bits';
import { Segmented, Sheet } from '@/ui/form/fields';
import { DayClock, SoftBars } from '@/rooms/heart/charts';
import '@/rooms/heart/heart.css';
import './ledger.css';

type Range = 'today' | 'week' | 'month' | 'all';

/** 引擎任务名的「人话」 */
const TASK: Record<string, string> = {
  response_generate: '聊天回话',
  cognition_analyze: '感受心情',
  memory_extract: '整理回忆',
  intent_analyze: '揣摩意思',
  autonomy_think: '自己想事情',
  autonomy_generate: '自己的事',
  life_plan: '排今天的日程',
  life_reflect: '睡前回想这一天',
  life_dream: '做梦',
  image_caption: '看图',
  image_generate: '画画',
  event_extract: '记下发生的事',
  embedding: '回忆检索',
  work_mode: '认真工作',
};
const taskName = (t: string) => TASK[t] ?? t;

const SECTION: Record<string, string> = {
  system_prompt_total: '人设和背景',
  user_message: '大家说的话',
  output_total: '她说的话',
};

const COLORS = ['#ffb86e', '#f78aa8', '#93a8ff', '#8fe0c0', '#c6a4ff', '#ffd27a', '#8ec5f0'];

function rangeOf(r: Range): { start?: number; end?: number } {
  const now = Date.now() / 1000;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  if (r === 'today') return { start: today.getTime() / 1000, end: now };
  if (r === 'week') return { start: now - 7 * 86400, end: now };
  if (r === 'month') return { start: now - 30 * 86400, end: now };
  return {};
}

const fmt = (n: number) =>
  n >= 1e8
    ? `${(n / 1e8).toFixed(2)} 亿`
    : n >= 1e4
      ? `${(n / 1e4).toFixed(1)} 万`
      : Math.round(n).toLocaleString('zh-CN');

/** 星光走势：按小时或按天把用量画成一条柔和的山丘 */
function Trend({ hourly, byDay }: { hourly: TokenReport['hourly']; byDay: boolean }) {
  const [hover, setHover] = useState<number | null>(null);
  const points = useMemo(() => {
    if (!byDay) return hourly.map((h) => ({ t: h.hour_ts, v: h.total_tokens, calls: h.calls }));
    const days = new Map<string, { t: number; v: number; calls: number }>();
    for (const h of hourly) {
      const d = new Date(h.hour_ts * 1000);
      d.setHours(0, 0, 0, 0);
      const key = d.toISOString();
      const cur = days.get(key) ?? { t: d.getTime() / 1000, v: 0, calls: 0 };
      cur.v += h.total_tokens;
      cur.calls += h.calls;
      days.set(key, cur);
    }
    return [...days.values()].sort((a, b) => a.t - b.t);
  }, [hourly, byDay]);

  if (points.length < 2) return <p className="field__hint">数据还太少，画不出曲线。</p>;
  const W = 640;
  const H = 180;
  const max = Math.max(...points.map((p) => p.v)) || 1;
  const x = (i: number) => 10 + (i / (points.length - 1)) * (W - 20);
  const y = (v: number) => H - 22 - (v / max) * (H - 40);
  const line = points.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.v).toFixed(1)}`).join('');
  const hp = hover !== null ? points[hover] : null;

  return (
    <div className="trend">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="trend__svg"
        onPointerMove={(e) => {
          const r = (e.currentTarget as SVGSVGElement).getBoundingClientRect();
          const i = Math.round(
            (((e.clientX - r.left) / r.width) * W - 10) / ((W - 20) / (points.length - 1)),
          );
          setHover(Math.max(0, Math.min(points.length - 1, i)));
        }}
        onPointerLeave={() => setHover(null)}
      >
        <defs>
          <linearGradient id="trend-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#ffd27a" stopOpacity="0.6" />
            <stop offset="100%" stopColor="#ffd27a" stopOpacity="0" />
          </linearGradient>
        </defs>
        <motion.path
          d={`${line}L${x(points.length - 1)},${H - 22}L${x(0)},${H - 22}Z`}
          fill="url(#trend-fill)"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
        />
        <motion.path
          d={line}
          fill="none"
          stroke="#f0a040"
          strokeWidth="2.5"
          strokeLinejoin="round"
          initial={{ pathLength: 0 }}
          animate={{ pathLength: 1 }}
          transition={{ duration: 1.2, ease: [0.22, 1, 0.36, 1] }}
        />
        {hover !== null && (
          <>
            <line x1={x(hover)} x2={x(hover)} y1={10} y2={H - 22} stroke="#e8628a" strokeDasharray="3 4" />
            <circle
              cx={x(hover)}
              cy={y(points[hover].v)}
              r="5"
              fill="#fff"
              stroke="#e8628a"
              strokeWidth="2.5"
            />
          </>
        )}
        <text x={10} y={H - 4} className="trend__axis">
          {new Date(points[0].t * 1000).toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' })}
        </text>
        <text x={W - 10} y={H - 4} textAnchor="end" className="trend__axis">
          {byDay ? '今天' : '现在'}
        </text>
      </svg>
      <p className="field__hint trend__tip">
        {hp
          ? `${new Date(hp.t * 1000).toLocaleString('zh-CN', byDay ? { month: 'numeric', day: 'numeric' } : { month: 'numeric', day: 'numeric', hour: '2-digit' })}：${fmt(hp.v)} 星光 · ${hp.calls} 次`
          : '把鼠标放在曲线上看看那一刻'}
      </p>
    </div>
  );
}

function Donut({ parts }: { parts: { label: string; value: number; color: string }[] }) {
  const total = parts.reduce((s, p) => s + p.value, 0) || 1;
  let acc = 0;
  const r = 46;
  const c = 2 * Math.PI * r;
  return (
    <div className="donut">
      <svg viewBox="0 0 120 120" width="150" height="150">
        <circle cx="60" cy="60" r={r} fill="none" stroke="#eef1fb" strokeWidth="16" />
        {parts.map((p) => {
          const len = (p.value / total) * c;
          const el = (
            <motion.circle
              key={p.label}
              cx="60"
              cy="60"
              r={r}
              fill="none"
              stroke={p.color}
              strokeWidth="16"
              strokeDasharray={`${len} ${c - len}`}
              strokeDashoffset={-acc}
              transform="rotate(-90 60 60)"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
            />
          );
          acc += len;
          return el;
        })}
      </svg>
      <ul>
        {parts.map((p) => (
          <li key={p.label}>
            <i style={{ background: p.color }} />
            <span>{p.label}</span>
            <b className="num">{Math.round((p.value / total) * 100)}%</b>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Rows({ rows, label }: { rows: UsageRow[]; label: (n: string) => string }) {
  return (
    <SoftBars
      items={rows
        .slice(0, 8)
        .map((r) => ({ key: r.name || '—', label: label(r.name), value: r.total_tokens }))}
      color={(k) => COLORS[Math.abs([...k].reduce((h, ch) => h * 31 + ch.charCodeAt(0), 0)) % COLORS.length]}
    />
  );
}

/** 小账本：她吃了多少星光（token）、花在哪儿、什么时候吃得最多 */
export function Ledger() {
  const persona = useCurrentPersona();
  const [range, setRange] = useState<Range>('week');
  const r = useMemo(() => rangeOf(range), [range]);
  const { data, isLoading, isFetching } = useTokens(r);
  if (!persona) return null;
  const name = persona.persona_name || persona.name;
  const s = data?.summary;
  const change = data?.period_comparison?.change_total_tokens;
  const rangeLabel = { today: '今天', week: '这 7 天', month: '这 30 天', all: '一直以来' }[range];

  return (
    <motion.div
      className="room-column room-column--wide ledger"
      initial={{ opacity: 0, y: 30 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: 20 }}
      transition={{ type: 'spring', stiffness: 120, damping: 20 }}
    >
      <div className="room-title">
        <div>
          <h1 className="hand">小账本</h1>
          <p>{name}每说一句话、每想一件事都要吃一点星光（token）。这里记着她吃了多少。</p>
        </div>
        <Segmented
          value={range}
          onChange={setRange}
          options={[
            { value: 'today', label: '今天' },
            { value: 'week', label: '7 天' },
            { value: 'month', label: '30 天' },
            { value: 'all', label: '全部' },
          ]}
        />
      </div>

      {isLoading && <p className="mirror__loading hand">翻账本……</p>}
      {s && (
        <>
          <section className={`sheet paper ledger__head${isFetching ? ' is-busy' : ''}`}>
            <p className="ledger__kicker">{rangeLabel}她一共吃了</p>
            <p className="ledger__big">
              <CountUp value={s.total_tokens} className="ledger__n" />
              <span className="hand">颗星光</span>
            </p>
            <p className="ledger__sub">
              说了 <b className="num">{s.total_calls.toLocaleString('zh-CN')}</b> 次话 · 平均每次{' '}
              <b className="num">{fmt(data!.response_avg?.avg_total_tokens ?? 0)}</b>
              {range !== 'all' && typeof change === 'number' && (
                <span className={change > 0 ? 'up' : 'down'}>
                  {' '}
                  · 比上一段{change > 0 ? '多' : '少'}了 {Math.abs(change).toFixed(0)}%
                </span>
              )}
            </p>
            <div className="ledger__tiles">
              <span>
                <b className="num">{(data!.cache_stats?.cache_hit_rate_pct ?? 0).toFixed(0)}%</b>
                省下的（缓存命中）
              </span>
              <span>
                <b className="num">{fmt(s.total_prompt_tokens)}</b>
                读进去的
              </span>
              <span>
                <b className="num">{fmt(s.total_completion_tokens)}</b>
                说出来的
              </span>
              <span>
                <b className="num">{((data!.duration_stats?.overall.avg_ms ?? 0) / 1000).toFixed(1)}s</b>
                平均想多久
              </span>
              <span className={(data!.failure_stats?.failure_rate_pct ?? 0) > 3 ? 'is-warn' : ''}>
                <b className="num">{(data!.failure_stats?.failure_rate_pct ?? 0).toFixed(1)}%</b>
                没吃成（失败）
              </span>
              <span>
                <b className="num">{(data!.retry_stats?.retry_rate_pct ?? 0).toFixed(1)}%</b>
                重来一次
              </span>
            </div>
          </section>

          <Sheet title="什么时候吃的">
            <Trend hourly={data!.hourly ?? []} byDay={range !== 'today'} />
          </Sheet>

          <div className="heart__pair">
            <Sheet title="一天里的饭点">
              <DayClock
                hours={Object.fromEntries(
                  (data!.hourly_distribution ?? []).map((h) => [String(h.hour), h.calls]),
                )}
              />
            </Sheet>
            <Sheet title="都吃进了什么">
              <Donut
                parts={Object.entries(data!.section_breakdown ?? {})
                  .filter(([k]) => k !== 'total')
                  .map(([k, v], i) => ({
                    label: SECTION[k] ?? k,
                    value: v,
                    color: COLORS[i % COLORS.length],
                  }))}
              />
            </Sheet>
          </div>

          <Sheet title="花在哪些事上">
            <Rows rows={data!.by_task ?? []} label={taskName} />
          </Sheet>

          {(data!.by_group ?? []).length > 0 && (
            <Sheet title="在哪个群吃得多" fold>
              <Rows rows={data!.by_group} label={(g) => (g ? `群 ${g}` : '私聊和独处')} />
            </Sheet>
          )}

          <Sheet title="最近几笔" fold>
            <ul className="ledger__recent">
              {(data!.recent_with_breakdown ?? []).slice(0, 20).map((rec) => (
                <li key={rec.id} className={rec.error_type ? 'is-fail' : ''}>
                  <span className="hand">{taskName(rec.task_name)}</span>
                  <span className="num">{rec.total_tokens.toLocaleString('zh-CN')}</span>
                  <em>
                    {rec.group_id ? `群 …${rec.group_id.slice(-4)}` : '私聊/独处'} ·{' '}
                    {(rec.duration_ms / 1000).toFixed(1)}s ·{' '}
                    {ago(new Date(rec.timestamp * 1000).toISOString())}
                    {rec.error_type && ` · ${rec.error_type}`}
                  </em>
                </li>
              ))}
            </ul>
          </Sheet>
        </>
      )}
    </motion.div>
  );
}
