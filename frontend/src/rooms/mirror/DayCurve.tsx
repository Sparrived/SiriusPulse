import { useMemo, useRef, useState } from 'react';
import type { CurvePoint } from '@/lib/types';

const W = 480;
const H = 210;
const PAD = { l: 34, r: 12, t: 16, b: 30 };
const MAX = 2;

const toMin = (t: string) => {
  const [h, m] = t.split(':').map(Number);
  return h * 60 + m;
};
const toTime = (min: number) => {
  const m = Math.max(0, Math.min(1440, Math.round(min / 15) * 15));
  return m === 1440
    ? '24:00'
    : `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;
};
const x = (min: number) => PAD.l + (min / 1440) * (W - PAD.l - PAD.r);
const y = (c: number) => PAD.t + (1 - c / MAX) * (H - PAD.t - PAD.b);

/** 与后端 get_reply_time_coefficient 一致：点之间线性插值，跨过午夜首尾相连 */
export function coefficientAt(points: CurvePoint[], minute: number): number {
  if (!points.length) return 1;
  if (points.length === 1) return points[0].coefficient;
  const anchors = points.map((p) => [toMin(p.time), p.coefficient] as const).sort((a, b) => a[0] - b[0]);
  for (let i = 0; i < anchors.length - 1; i++) {
    const [lm, lv] = anchors[i];
    const [rm, rv] = anchors[i + 1];
    if (minute >= lm && minute <= rm) return rm === lm ? lv : lv + ((rv - lv) * (minute - lm)) / (rm - lm);
  }
  const [lm, lv] = anchors[anchors.length - 1];
  const [rm, rv] = anchors[0];
  const cur = minute < rm ? minute + 1440 : minute;
  return lv + ((rv - lv) * (cur - lm)) / (rm + 1440 - lm);
}

function describe(c: number) {
  if (c < 0.3) return '几乎不说话';
  if (c < 0.8) return '懒洋洋的';
  if (c < 1.2) return '平常的样子';
  if (c < 1.6) return '比较来劲';
  return '超级活跃';
}

/**
 * 一天的精神曲线：横轴是一天 24 小时，纵轴是回话的积极程度（1 = 平常）。
 * 双击空白处加一个时间点，拖动调整，双击点删除。
 */
export function DayCurve({
  points,
  onChange,
}: {
  points: CurvePoint[];
  onChange: (p: CurvePoint[]) => void;
}) {
  const svg = useRef<SVGSVGElement>(null);
  const [drag, setDrag] = useState<number | null>(null);
  const [hover, setHover] = useState<number | null>(null);
  const sorted = useMemo(() => [...points].sort((a, b) => toMin(a.time) - toMin(b.time)), [points]);

  const path = useMemo(() => {
    const parts: string[] = [];
    for (let m = 0; m <= 1440; m += 10)
      parts.push(`${m ? 'L' : 'M'}${x(m).toFixed(1)},${y(coefficientAt(sorted, m)).toFixed(1)}`);
    return parts.join('');
  }, [sorted]);

  const local = (e: React.PointerEvent | React.MouseEvent) => {
    const rect = svg.current!.getBoundingClientRect();
    const px = ((e.clientX - rect.left) / rect.width) * W;
    const py = ((e.clientY - rect.top) / rect.height) * H;
    const min = ((px - PAD.l) / (W - PAD.l - PAD.r)) * 1440;
    const c = (1 - (py - PAD.t) / (H - PAD.t - PAD.b)) * MAX;
    return { min: Math.max(0, Math.min(1440, min)), c: Math.round(Math.max(0, Math.min(MAX, c)) * 20) / 20 };
  };

  const update = (index: number, e: React.PointerEvent) => {
    const { min, c } = local(e);
    const time = toTime(min);
    const next = sorted.map((p, i) => (i === index ? { time, coefficient: c } : p));
    // 拖到别的点所在的时刻上就不动时间，只改高度，免得两个点叠在一起
    if (next.some((p, i) => i !== index && p.time === time))
      next[index] = { ...sorted[index], coefficient: c };
    onChange(next);
  };

  const add = (e: React.MouseEvent) => {
    if (drag !== null) return;
    const { min, c } = local(e);
    const time = toTime(min);
    if (sorted.some((p) => p.time === time)) return;
    onChange([...sorted, { time, coefficient: c }]);
  };

  const now = new Date();
  const nowMin = now.getHours() * 60 + now.getMinutes();
  const hoverMin = hover;

  return (
    <div className="curve">
      <svg
        ref={svg}
        viewBox={`0 0 ${W} ${H}`}
        className="curve__svg"
        onDoubleClick={add}
        onPointerMove={(e) => {
          if (drag !== null) update(drag, e);
          setHover(local(e).min);
        }}
        onPointerLeave={() => setHover(null)}
        onPointerUp={() => setDrag(null)}
      >
        <defs>
          <linearGradient id="curve-sky" x1="0" x2="1">
            <stop offset="0%" stopColor="#1c2453" />
            <stop offset="22%" stopColor="#3b3f7c" />
            <stop offset="30%" stopColor="#ffcdc4" />
            <stop offset="40%" stopColor="#b5cbf6" />
            <stop offset="70%" stopColor="#b5cbf6" />
            <stop offset="78%" stopColor="#ffb59d" />
            <stop offset="86%" stopColor="#3b3f7c" />
            <stop offset="100%" stopColor="#1c2453" />
          </linearGradient>
          <linearGradient id="curve-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#ff9fb8" stopOpacity="0.55" />
            <stop offset="100%" stopColor="#ff9fb8" stopOpacity="0" />
          </linearGradient>
        </defs>
        <rect
          x={PAD.l}
          y={PAD.t}
          width={W - PAD.l - PAD.r}
          height={H - PAD.t - PAD.b}
          rx="12"
          fill="url(#curve-sky)"
          opacity="0.32"
        />
        {[0, 1, 2].map((c) => (
          <g key={c}>
            <line
              x1={PAD.l}
              x2={W - PAD.r}
              y1={y(c)}
              y2={y(c)}
              stroke={c === 1 ? '#4f6fd8' : '#c9d1f2'}
              strokeDasharray={c === 1 ? '4 4' : '2 5'}
              opacity={c === 1 ? 0.6 : 1}
            />
            <text x={PAD.l - 6} y={y(c) + 4} textAnchor="end" className="curve__axis">
              {['静', '平常', '活跃'][c]}
            </text>
          </g>
        ))}
        {[0, 6, 12, 18, 24].map((h) => (
          <text key={h} x={x(h * 60)} y={H - 10} textAnchor="middle" className="curve__axis">
            {h === 0 ? '午夜' : h === 6 ? '清晨' : h === 12 ? '正午' : h === 18 ? '傍晚' : '午夜'}
          </text>
        ))}
        <line
          x1={x(nowMin)}
          x2={x(nowMin)}
          y1={PAD.t}
          y2={H - PAD.b}
          stroke="#f78aa8"
          strokeWidth="1.5"
          opacity="0.7"
        />
        <text x={x(nowMin) + 4} y={PAD.t + 12} className="curve__now">
          现在
        </text>
        <path d={`${path}L${x(1440)},${y(0)}L${x(0)},${y(0)}Z`} fill="url(#curve-fill)" />
        <path d={path} fill="none" stroke="#e8628a" strokeWidth="2.5" strokeLinecap="round" />
        {sorted.map((p, i) => (
          <circle
            key={p.time}
            cx={x(toMin(p.time))}
            cy={y(p.coefficient)}
            r={drag === i ? 9 : 7}
            className="curve__point"
            onPointerDown={(e) => {
              e.stopPropagation();
              (e.target as Element).setPointerCapture(e.pointerId);
              setDrag(i);
            }}
            onDoubleClick={(e) => {
              e.stopPropagation();
              onChange(sorted.filter((_, j) => j !== i));
            }}
          >
            <title>
              {p.time} · {describe(p.coefficient)}（双击删除）
            </title>
          </circle>
        ))}
        {hoverMin !== null && drag === null && (
          <text
            x={x(hoverMin)}
            y={y(coefficientAt(sorted, hoverMin)) - 12}
            textAnchor="middle"
            className="curve__tip"
          >
            {toTime(hoverMin)} {describe(coefficientAt(sorted, hoverMin))}
          </text>
        )}
      </svg>
      <p className="field__hint">
        双击空白处加一个时间点，拖着点上下左右调，双击点把它删掉。此刻她
        {describe(coefficientAt(sorted, nowMin))}。
      </p>
    </div>
  );
}
