import { motion } from 'motion/react';
import { useRef } from 'react';

const CORNERS = [
  { x: 0, y: 1, text: '沉稳但健谈' },
  { x: 1, y: 1, text: '热情小话痨' },
  { x: 0, y: 0, text: '安静又腼腆' },
  { x: 1, y: 0, text: '活泼但矜持' },
];

function describe(x: number, y: number): string {
  if (Math.abs(x - 0.5) < 0.15 && Math.abs(y - 0.5) < 0.15) return '刚刚好的她';
  const near = CORNERS.reduce((best, c) =>
    Math.hypot(c.x - x, c.y - y) < Math.hypot(best.x - x, best.y - y) ? c : best,
  );
  return Math.hypot(near.x - x, near.y - y) < 0.45 ? near.text : '有一点点' + near.text;
}

/**
 * 性格小罗盘：横轴「活泼程度」(expressiveness)，纵轴「爱不爱插话」(engagement_sensitivity)。
 * 拖动小星星，两个数值一起变——比两条滑杆更像在描述一个人。
 */
export function PersonalityPad({
  expressiveness,
  engagement,
  onChange,
}: {
  expressiveness: number;
  engagement: number;
  onChange: (v: { expressiveness: number; engagement_sensitivity: number }) => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const dragging = useRef(false);

  const fromPointer = (e: React.PointerEvent) => {
    const rect = box.current!.getBoundingClientRect();
    const snap = (v: number) => Math.round(Math.min(1, Math.max(0, v)) * 20) / 20;
    onChange({
      expressiveness: snap((e.clientX - rect.left) / rect.width),
      engagement_sensitivity: snap(1 - (e.clientY - rect.top) / rect.height),
    });
  };

  const onKey = (e: React.KeyboardEvent) => {
    const step = e.shiftKey ? 0.1 : 0.05;
    const move: Record<string, [number, number]> = {
      ArrowLeft: [-step, 0],
      ArrowRight: [step, 0],
      ArrowUp: [0, step],
      ArrowDown: [0, -step],
    };
    const d = move[e.key];
    if (!d) return;
    e.preventDefault();
    const clamp = (v: number) => Math.round(Math.min(1, Math.max(0, v)) * 100) / 100;
    onChange({
      expressiveness: clamp(expressiveness + d[0]),
      engagement_sensitivity: clamp(engagement + d[1]),
    });
  };

  return (
    <div className="pad">
      <div
        ref={box}
        className="pad__field"
        role="slider"
        tabIndex={0}
        aria-label="性格罗盘：方向键调整"
        aria-valuetext={`活泼 ${Math.round(expressiveness * 100)}%，爱插话 ${Math.round(engagement * 100)}%`}
        onPointerDown={(e) => {
          dragging.current = true;
          (e.target as Element).setPointerCapture?.(e.pointerId);
          fromPointer(e);
        }}
        onPointerMove={(e) => dragging.current && fromPointer(e)}
        onPointerUp={() => (dragging.current = false)}
        onKeyDown={onKey}
      >
        {CORNERS.map((c) => (
          <span key={c.text} className={`pad__corner pad__corner--${c.x}${c.y} hand`}>
            {c.text}
          </span>
        ))}
        <span className="pad__axis pad__axis--x" />
        <span className="pad__axis pad__axis--y" />
        <motion.span
          className="pad__star"
          animate={{ left: `${expressiveness * 100}%`, top: `${(1 - engagement) * 100}%` }}
          transition={{ type: 'spring', stiffness: 500, damping: 34 }}
        >
          ✦
        </motion.span>
      </div>
      <div className="pad__legend">
        <span className="hand pad__desc">{describe(expressiveness, engagement)}</span>
        <span className="pad__nums">
          活泼 <b className="num">{Math.round(expressiveness * 100)}</b> · 爱插话{' '}
          <b className="num">{Math.round(engagement * 100)}</b>
        </span>
      </div>
    </div>
  );
}
