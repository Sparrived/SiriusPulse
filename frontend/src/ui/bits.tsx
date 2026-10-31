import { animate, useInView } from 'motion/react';
import { useEffect, useRef } from 'react';

/** 数字从 0 慢慢数上来 */
export function CountUp({ value, className }: { value: number; className?: string }) {
  const el = useRef<HTMLSpanElement>(null);
  const seen = useInView(el, { once: true });
  const from = useRef(0);
  useEffect(() => {
    if (!seen || !el.current) return;
    const controls = animate(from.current, value, {
      duration: 1.1,
      ease: [0.22, 1, 0.36, 1],
      onUpdate: (v) => {
        if (el.current) el.current.textContent = Math.round(v).toLocaleString('zh-CN');
      },
    });
    from.current = value;
    return () => controls.stop();
  }, [value, seen]);
  return (
    <span ref={el} className={`num ${className ?? ''}`}>
      0
    </span>
  );
}

/** 心情小罗盘：横轴是愉快程度，纵轴是精神头；虚线圈是她平时的样子。 */
export function MoodCompass({
  valence,
  arousal,
  baseValence,
  baseArousal,
  size = 92,
}: {
  valence: number;
  arousal: number;
  baseValence: number;
  baseArousal: number;
  size?: number;
}) {
  const r = size / 2 - 6;
  const c = size / 2;
  const px = (v: number) => c + v * r * 0.82;
  const py = (a: number) => c - (a * 2 - 1) * r * 0.82;
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img" aria-label="心情罗盘">
      <defs>
        <radialGradient id="mc-bg" cx="50%" cy="45%" r="60%">
          <stop offset="0%" stopColor="#ffffff" />
          <stop offset="100%" stopColor="#e6ebff" />
        </radialGradient>
        <linearGradient id="mc-warm" x1="0" x2="1">
          <stop offset="0%" stopColor="#8f9bd6" stopOpacity="0.35" />
          <stop offset="50%" stopColor="#ffffff" stopOpacity="0" />
          <stop offset="100%" stopColor="#ff9fb8" stopOpacity="0.4" />
        </linearGradient>
      </defs>
      <circle cx={c} cy={c} r={r} fill="url(#mc-bg)" stroke="#fff" strokeWidth="2" />
      <circle cx={c} cy={c} r={r} fill="url(#mc-warm)" />
      <path
        d={`M${c - r + 6} ${c}H${c + r - 6}M${c} ${c - r + 6}V${c + r - 6}`}
        stroke="#c9d1f2"
        strokeDasharray="2 4"
      />
      <circle
        cx={px(baseValence)}
        cy={py(baseArousal)}
        r="7"
        fill="none"
        stroke="#9aa6d8"
        strokeDasharray="2 3"
      />
      <circle cx={px(valence)} cy={py(arousal)} r="6" fill="#ff8fae" stroke="#fff" strokeWidth="2.5">
        <animate attributeName="r" values="6;7.2;6" dur="2.8s" repeatCount="indefinite" />
      </circle>
    </svg>
  );
}
