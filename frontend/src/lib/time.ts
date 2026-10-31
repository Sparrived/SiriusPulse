export type DayPhase = 'dawn' | 'day' | 'dusk' | 'night';

export function dayPhase(date = new Date()): DayPhase {
  const h = date.getHours() + date.getMinutes() / 60;
  if (h >= 5 && h < 7.5) return 'dawn';
  if (h >= 7.5 && h < 17) return 'day';
  if (h >= 17 && h < 19.5) return 'dusk';
  return 'night';
}

/** 0 = 深夜，1 = 正午；用于天空插值 */
export function daylight(date = new Date()): number {
  const h = date.getHours() + date.getMinutes() / 60;
  const x = Math.cos(((h - 13) / 24) * Math.PI * 2);
  return Math.max(0, Math.min(1, (x + 0.25) / 1.1));
}

/** 她对你说的第一句话 */
export function greeting(date = new Date()): string {
  const h = date.getHours();
  if (h < 5) return '这么晚还不睡呀';
  if (h < 9) return '早上好呀';
  if (h < 12) return '上午好';
  if (h < 14) return '中午好，记得吃饭哦';
  if (h < 18) return '下午好';
  if (h < 22) return '晚上好，欢迎回家';
  return '夜深了，欢迎回来';
}

export function ago(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return '';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '';
  const s = Math.max(0, (now - t) / 1000);
  if (s < 45) return '刚刚';
  if (s < 3600) return `${Math.round(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.round(s / 3600)} 小时前`;
  const d = Math.round(s / 86400);
  if (d < 30) return `${d} 天前`;
  return new Date(t).toLocaleDateString('zh-CN', { month: 'long', day: 'numeric' });
}

export function duration(seconds: number): string {
  if (!seconds || seconds < 60) return '刚刚醒来';
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m} 分钟`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h} 小时 ${m % 60} 分`;
  return `${Math.floor(h / 24)} 天 ${h % 24} 小时`;
}
