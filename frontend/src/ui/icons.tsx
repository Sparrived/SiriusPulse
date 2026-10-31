import type { SVGProps } from 'react';

// 手绘感图标：圆头线条、略带不规则的弧度，和她的画风站在一起。
type P = SVGProps<SVGSVGElement>;
const base = {
  width: 26,
  height: 26,
  viewBox: '0 0 32 32',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.9,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
};

export const HomeIcon = (p: P) => (
  <svg {...base} {...p}>
    <path d="M5 15.5 16 6l11 9.5" />
    <path d="M8 13.5V26h16V13.5" />
    <path d="M16 22.5s-3.6-2.1-3.6-4.4c0-1.3 1-2.2 2.1-2.2.7 0 1.2.4 1.5.9.3-.5.8-.9 1.5-.9 1.1 0 2.1.9 2.1 2.2 0 2.3-3.6 4.4-3.6 4.4Z" />
  </svg>
);

export const StarsIcon = (p: P) => (
  <svg {...base} {...p}>
    <path d="m9 7 .9 2.2L12 10l-2.1.8L9 13l-.9-2.2L6 10l2.1-.8Z" />
    <path d="m23 13 1.2 2.8L27 17l-2.8 1.2L23 21l-1.2-2.8L19 17l2.8-1.2Z" />
    <circle cx="12" cy="24" r="1.4" />
    <path d="M10.5 12.5 12 22.6M13.3 23.4l8-5" strokeDasharray="1.5 2.6" />
  </svg>
);

export const MirrorIcon = (p: P) => (
  <svg {...base} {...p}>
    <ellipse cx="16" cy="13" rx="7.5" ry="9" />
    <path d="M16 22v5M11.5 27h9" />
    <path d="M12.5 10.5c.6-2 2-3.3 3.8-3.6" />
  </svg>
);

export const DiaryIcon = (p: P) => (
  <svg {...base} {...p}>
    <path d="M8 5.5h14.5a1.5 1.5 0 0 1 1.5 1.5v19.5H9.5A1.5 1.5 0 0 1 8 25Z" />
    <path d="M8 25a1.5 1.5 0 0 1 1.5-1.5H24" />
    <path d="M19 5.5v8l2-1.6 2 1.6" />
    <path d="M12 11h4M12 15h5" />
  </svg>
);

export const JarIcon = (p: P) => (
  <svg {...base} {...p}>
    <path d="M11 5h10M12 5v3.5c-3 1.6-5 4.3-5 8.5v6.5A2.5 2.5 0 0 0 9.5 26h13a2.5 2.5 0 0 0 2.5-2.5V17c0-4.2-2-6.9-5-8.5V5" />
    <circle cx="13" cy="18" r="1" fill="currentColor" />
    <circle cx="19" cy="15.5" r="1.2" fill="currentColor" />
    <circle cx="17.5" cy="21.5" r="0.9" fill="currentColor" />
  </svg>
);

export const WeatherIcon = (p: P) => (
  <svg {...base} {...p}>
    <path d="M9.5 22.5a4.5 4.5 0 0 1-.4-9 6.5 6.5 0 0 1 12.6 1.4 3.8 3.8 0 0 1 .3 7.6Z" />
    <path d="M16 20s-2.4-1.4-2.4-2.9c0-.9.7-1.5 1.4-1.5.5 0 .8.3 1 .6.2-.3.5-.6 1-.6.7 0 1.4.6 1.4 1.5 0 1.5-2.4 2.9-2.4 2.9Z" />
    <path d="M11 26.5l-.6 1.5M16 26.5l-.6 1.5M21 26.5l-.6 1.5" />
  </svg>
);

export const BagIcon = (p: P) => (
  <svg {...base} {...p}>
    <path d="M9 12a7 7 0 0 1 14 0v12.5a2 2 0 0 1-2 2H11a2 2 0 0 1-2-2Z" />
    <path d="M13 7.2V6a3 3 0 0 1 6 0v1.2" />
    <path d="M9 17h14M14 17v3h4v-3" />
  </svg>
);

export const MailIcon = (p: P) => (
  <svg {...base} {...p}>
    <rect x="5" y="9" width="22" height="15" rx="2.5" />
    <path d="m6 10.5 10 7.5 10-7.5" />
  </svg>
);

export const LedgerIcon = (p: P) => (
  <svg {...base} {...p}>
    <path d="M7 7.5h13.5L25 12v13.5H7Z" />
    <path d="M20.5 7.5V12H25" />
    <path d="M11 16h9M11 20h6" />
  </svg>
);

export const AtticIcon = (p: P) => (
  <svg {...base} {...p}>
    <path d="M16 4.5v3" />
    <path d="M11.5 9.5h9l-1 3h-7Z" />
    <path d="M12 12.5c-1.2 1.6-1.8 3.6-1.8 5.6 0 3.6 2.6 6.4 5.8 6.4s5.8-2.8 5.8-6.4c0-2-.6-4-1.8-5.6" />
    <path d="M16 15.5c-1.2 1.2-1.6 2.3-1.6 3.2 0 .9.7 1.6 1.6 1.6s1.6-.7 1.6-1.6c0-.9-.4-2-1.6-3.2Z" />
    <path d="M13 27.5h6" />
  </svg>
);

export const MoonIcon = (p: P) => (
  <svg {...base} {...p}>
    <path d="M21.5 20.5A8.5 8.5 0 0 1 12 8.2a8.5 8.5 0 1 0 11.8 11.4 8.4 8.4 0 0 1-2.3.9Z" />
  </svg>
);

export const SunIcon = (p: P) => (
  <svg {...base} {...p}>
    <circle cx="16" cy="16" r="5" />
    <path d="M16 5v2.5M16 24.5V27M5 16h2.5M24.5 16H27M8.2 8.2l1.8 1.8M22 22l1.8 1.8M8.2 23.8 10 22M22 10l1.8-1.8" />
  </svg>
);
