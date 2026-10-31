import * as THREE from 'three';

export const EMOTION_CN: Record<string, string> = {
  JOY: '喜悦',
  CONTENTMENT: '满足',
  RELIEF: '释然',
  EXCITEMENT: '兴奋',
  SADNESS: '悲伤',
  GRIEF: '悲痛',
  ANGER: '愤怒',
  IRRITATION: '恼怒',
  ANXIETY: '焦虑',
  FEAR: '恐惧',
  DISGUST: '厌恶',
  SURPRISE: '惊讶',
  TRUST: '信任',
  ANTICIPATION: '期待',
  LOVE: '喜爱',
  LONELINESS: '孤独',
  GRATITUDE: '感激',
  HOPE: '希望',
  NEUTRAL: '平静',
};

/** 她自己口吻的心情描述 */
export function moodLine(valence: number, arousal: number): string {
  if (valence > 0.5) return arousal > 0.6 ? '心情好得想转圈圈' : '心里暖洋洋的';
  if (valence > 0.15) return arousal > 0.6 ? '有点小雀跃' : '安安稳稳，挺好的';
  if (valence > -0.2) return arousal > 0.6 ? '有点心神不定' : '平平静静的';
  if (valence > -0.5) return arousal > 0.6 ? '有点烦躁，需要抱抱' : '有一点点低落';
  return arousal > 0.6 ? '心里乱糟糟的' : '不太开心……陪陪我好吗';
}

const WARM = new THREE.Color('#ffb3c7');
const CALM = new THREE.Color('#9fb8ff');
const BLUE = new THREE.Color('#6d7fd6');
const GREY = new THREE.Color('#8a8fa8');

/** 心情 → 天空的点缀色：愉快偏粉暖，平静偏月白蓝，低落偏灰。 */
export function moodTint(valence: number): THREE.Color {
  const c = new THREE.Color();
  if (valence >= 0.15) return c.copy(CALM).lerp(WARM, Math.min(1, (valence - 0.15) / 0.6));
  if (valence >= -0.2) return c.copy(BLUE).lerp(CALM, (valence + 0.2) / 0.35);
  return c.copy(GREY).lerp(BLUE, Math.max(0, (valence + 1) / 0.8));
}
