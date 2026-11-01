import { useMutation } from '@tanstack/react-query';
import { motion } from 'motion/react';
import { useState } from 'react';
import { useLocation } from 'wouter';
import { api } from '@/lib/api';
import { EMOTION_CN, moodLine } from '@/lib/mood';
import { queryClient, useGlanceOf, useWakeSleep } from '@/lib/queries';
import { ago } from '@/lib/time';
import type { PersonaSummary } from '@/lib/types';
import { usePersonaStore } from '@/state/persona';
import { Face } from '@/ui/PersonaTag';

/** 点中一颗星：看看她最近怎么样，去她家、叫醒她，或者（很少用到的）送别。 */
export function StarCard({ persona, onClose }: { persona: PersonaSummary; onClose: () => void }) {
  const name = persona.persona_name || persona.name;
  const { data: glance } = useGlanceOf(persona.name);
  const current = usePersonaStore((s) => s.current);
  const choose = usePersonaStore((s) => s.choose);
  const [, navigate] = useLocation();
  const wakeSleep = useWakeSleep();
  const [farewell, setFarewell] = useState(false);
  const [typed, setTyped] = useState('');

  const remove = useMutation({
    mutationFn: () => api.del(`/api/personas/${encodeURIComponent(persona.name)}`, { global: true }),
    onSuccess: () => {
      onClose();
      queryClient.invalidateQueries({ queryKey: ['personas'] });
    },
  });

  const mood = glance?.mood;
  const convo = glance?.conversation;
  // 后端不允许删除仍在启动名单里的人格；醒着的也先哄睡
  const canLeave = !persona.running && !persona.active;

  return (
    <motion.aside
      className="star-card paper"
      initial={{ opacity: 0, y: 30, scale: 0.96 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, y: 20, scale: 0.97 }}
      transition={{ type: 'spring', stiffness: 260, damping: 26 }}
    >
      <button className="star-card__close" onClick={onClose} aria-label="关上">
        ×
      </button>
      <header className="star-card__head">
        <Face persona={persona} size={64} />
        <div>
          <h2 className="hand">{name}</h2>
          <p>
            {persona.running ? '醒着' : '睡着了'} · 门牌号 <code>{persona.name}</code>
            {current === persona.name && ' · 你正在她家'}
          </p>
        </div>
      </header>

      <div className="star-card__glance">
        {mood ? (
          <p className="hand">
            {persona.running ? moodLine(mood.valence, mood.arousal) : '睡梦里'}
            <span className="chip">{EMOTION_CN[mood.emotion] ?? mood.emotion_cn}</span>
          </p>
        ) : (
          <p className="hand">还没有心情记录</p>
        )}
        <p className="star-card__stats">
          这一天听了 <b className="num">{convo?.heard ?? 0}</b> 句、回了{' '}
          <b className="num">{convo?.replied ?? 0}</b> 句 · 记得{' '}
          <b className="num">{glance?.memory.total ?? 0}</b> 件事
          {convo?.last_activity_at && ` · 上次说话 ${ago(convo.last_activity_at)}`}
        </p>
      </div>

      <div className="star-card__actions">
        <button
          className="soft-btn"
          onClick={() => {
            choose(persona.name);
            navigate('/');
          }}
        >
          去她家
        </button>
        <button
          className={`soft-btn ${persona.running ? 'ghost' : 'blush'}`}
          disabled={wakeSleep.isPending}
          onClick={() => wakeSleep.mutate({ name: persona.name, wake: !persona.running })}
        >
          {wakeSleep.isPending ? '稍等……' : persona.running ? '哄她睡觉' : '叫醒她'}
        </button>
        <button className="star-card__farewell" onClick={() => setFarewell((v) => !v)}>
          送别…
        </button>
      </div>
      {wakeSleep.isError && <p className="star-card__error">{(wakeSleep.error as Error).message}</p>}

      {farewell && (
        <motion.div
          className="star-card__danger"
          initial={{ opacity: 0, height: 0 }}
          animate={{ opacity: 1, height: 'auto' }}
        >
          {canLeave ? (
            <>
              <p>
                送别会删掉她的整个家：人设、记忆、聊天记录，全部，<b>找不回来</b>。真的要的话，输入她的门牌号{' '}
                <code>{persona.name}</code>：
              </p>
              <input
                className="input input--mono"
                value={typed}
                onChange={(e) => setTyped(e.target.value)}
                placeholder={persona.name}
              />
              <button
                className="soft-btn star-card__goodbye"
                disabled={typed !== persona.name || remove.isPending}
                onClick={() => remove.mutate()}
              >
                {remove.isPending ? '……' : `和${name}道别`}
              </button>
              {remove.isError && <p className="star-card__error">{(remove.error as Error).message}</p>}
            </>
          ) : (
            <p>她还醒着（或在开机名单里），先哄她睡觉，才能送别。</p>
          )}
        </motion.div>
      )}
    </motion.aside>
  );
}
