import { EMOTION_CN } from './mood';
import type { EngineEvent } from './types';
import type { ThoughtKind } from '@/state/live';

const PHASE_LINES: Record<string, string> = {
  observe: '先听听大家在说什么',
  decide: '要不要接话呢……',
  plan: '想想该怎么回',
  act: '动手做点什么',
  verify: '再确认一下',
  respond: '组织一下语言',
};

function groupLabel(id: unknown): string {
  const s = String(id ?? '');
  if (!s) return '私聊里';
  return `群 ${s.length > 6 ? `…${s.slice(-4)}` : s} 里`;
}

/** 把引擎事件翻译成她的「小念头」；返回 null 表示这个事件不值得说出来。 */
export function eventToThought(event: EngineEvent): [ThoughtKind, string] | null {
  const d = event.data ?? {};
  switch (event.type) {
    case 'perception_completed':
      return ['hear', `${groupLabel(d.group_id)}有人说话了`];
    case 'cognition_completed': {
      const emo = d.emotion?.basic_emotion;
      if (!emo || emo === 'NEUTRAL') return null;
      return ['feel', `感觉对方有点${EMOTION_CN[emo] ?? '不一样'}`];
    }
    case 'decision_completed':
      return d.strategy === 'pass' ? ['quiet', '这句我就不插话啦'] : null;
    case 'agent_turn_updated': {
      if (d.origin === 'self_initiated') {
        const seed = String(d.episode?.seed ?? '').slice(0, 40);
        return ['self', seed ? `自己去做了点事：${seed}` : '自己去做了点小事'];
      }
      const attempts: any[] = Array.isArray(d.tool_attempts) ? d.tool_attempts : [];
      const running = attempts.find((a) => a?.status === 'running' || a?.status === 'started');
      if (running?.tool_name) return ['tool', `用一下「${running.tool_name}」`];
      const line = PHASE_LINES[String(d.phase)];
      return line ? ['think', line] : null;
    }
    case 'execution_completed':
      if (d.has_reply) return ['reply', `在${groupLabel(d.group_id)}回好啦`];
      return null;
    case 'reminder_triggered':
      return ['remind', `到点提醒大家：${String(d.reply ?? '').slice(0, 40)}`];
    case 'delayed_response_triggered':
      return ['think', '刚才那句，我想好了再回'];
    default:
      return null;
  }
}
