/** 心事房间里的「人话」：把引擎的标签翻译成她会说的话 */

/** 与后端 BasicEmotion 一致的参考坐标 (valence, arousal) */
export const EMOTION_SPOTS: Record<string, [number, number]> = {
  JOY: [0.8, 0.7],
  CONTENTMENT: [0.6, 0.3],
  RELIEF: [0.5, 0.2],
  EXCITEMENT: [0.9, 0.9],
  SADNESS: [-0.8, 0.2],
  GRIEF: [-0.9, 0.3],
  ANGER: [-0.7, 0.9],
  IRRITATION: [-0.5, 0.6],
  ANXIETY: [-0.6, 0.8],
  FEAR: [-0.8, 0.9],
  DISGUST: [-0.6, 0.5],
  SURPRISE: [0.3, 0.9],
  TRUST: [0.7, 0.4],
  ANTICIPATION: [0.4, 0.6],
  LOVE: [0.9, 0.5],
  LONELINESS: [-0.7, 0.3],
  GRATITUDE: [0.8, 0.4],
  HOPE: [0.7, 0.5],
  NEUTRAL: [0, 0.3],
};

export const EMOTION_COLOR: Record<string, string> = {
  JOY: '#ffc857',
  CONTENTMENT: '#9ed8a8',
  RELIEF: '#a8e0d0',
  EXCITEMENT: '#ff9a5a',
  SADNESS: '#7d8fd6',
  GRIEF: '#6370b8',
  ANGER: '#e8607a',
  IRRITATION: '#f08a7a',
  ANXIETY: '#b28ad8',
  FEAR: '#8f72c8',
  DISGUST: '#9aa96a',
  SURPRISE: '#ffd98a',
  TRUST: '#8ec5f0',
  ANTICIPATION: '#ffb47a',
  LOVE: '#ff8fae',
  LONELINESS: '#8a9ac0',
  GRATITUDE: '#ffcf8a',
  HOPE: '#a8d8ff',
  NEUTRAL: '#c8cde3',
};

export const INTENT: Record<string, string> = {
  social: '闲聊',
  help_seeking: '想要帮忙',
  emotional: '倾诉心情',
  silent: '不需要回应',
  plugin_command: '使唤小工具',
};

export const STRATEGY: Record<string, string> = {
  immediate: '马上回',
  delayed: '想一想再回',
  silent: '不说话',
  proactive: '主动开口',
};

export const REASON: Record<string, string> = {
  addressed: '在跟她说话',
  explicit_mention: '有人点了她的名',
  keyword_mentioned: '提到了她的名字',
  private_chat: '私聊',
  reply_needed: '这句话需要回应',
  natural_join: '自然地插一句',
  below_participation_threshold: '没必要插话',
  reply_cooldown: '刚说过话，歇一会儿',
  pending_promoted_by_explicit_mention: '被点名，赶紧回',
  group_busy: '群里太热闹了',
};

export const HEAT: Record<string, string> = { cold: '冷清', warm: '正常', hot: '热闹' };

export type Sky = 'sun' | 'partly' | 'cloud' | 'drizzle' | 'rain';

/** 平均心情 → 天气 */
export function weather(avgValence: number, spread: number): { sky: Sky; text: string } {
  if (avgValence > 0.35) return { sky: 'sun', text: spread > 0.5 ? '晴，偶尔飘过几朵云' : '大晴天' };
  if (avgValence > 0.1) return { sky: 'partly', text: '多云转晴' };
  if (avgValence > -0.15) return { sky: 'cloud', text: spread > 0.5 ? '多云，时晴时雨' : '阴天，平平静静' };
  if (avgValence > -0.4) return { sky: 'drizzle', text: '飘着小雨' };
  return { sky: 'rain', text: '下着雨，大家心情不太好' };
}
