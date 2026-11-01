import { AnimatePresence, motion } from 'motion/react';
import { useEffect, useRef, useState } from 'react';
import { useAvatar, useCurrentPersona, usePersonas } from '@/lib/queries';
import type { PersonaSummary } from '@/lib/types';
import { usePersonaStore } from '@/state/persona';
import './persona-tag.css';

export function Face({ persona, size = 38 }: { persona: PersonaSummary; size?: number }) {
  const url = useAvatar(persona.name).data?.url;
  return (
    <span className="face" style={{ width: size, height: size }}>
      <span className="face__clip">{url && <img src={url} alt="" />}</span>
      <span className={`face__dot${persona.running ? ' is-awake' : ''}`} />
    </span>
  );
}

/** 左上角的名牌：现在在谁家。点开可以去别的伙伴家串门。 */
export function PersonaTag() {
  const persona = useCurrentPersona();
  const { data } = usePersonas();
  const choose = usePersonaStore((s) => s.choose);
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (e: PointerEvent) => {
      if (!box.current?.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener('pointerdown', close);
    return () => window.removeEventListener('pointerdown', close);
  }, [open]);

  if (!persona) return null;
  const others = data?.personas.filter((p) => p.name !== persona.name) ?? [];

  return (
    <div className="persona-tag" ref={box}>
      <button className="persona-tag__me paper" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        <Face persona={persona} />
        <span className="persona-tag__text">
          <span className="persona-tag__name hand">{persona.persona_name || persona.name}</span>
          <span className="persona-tag__state">{persona.running ? '醒着' : '睡着了'}</span>
        </span>
        {others.length > 0 && (
          <span className="persona-tag__caret" aria-hidden>
            ⌄
          </span>
        )}
      </button>
      <AnimatePresence>
        {open && others.length > 0 && (
          <motion.ul
            className="persona-tag__list paper"
            initial={{ opacity: 0, y: -8, scale: 0.96 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -6, scale: 0.97 }}
            transition={{ type: 'spring', stiffness: 420, damping: 30 }}
          >
            <li className="persona-tag__title hand">去谁家串门？</li>
            {others.map((p) => (
              <li key={p.name}>
                <button
                  onClick={() => {
                    choose(p.name);
                    setOpen(false);
                  }}
                >
                  <Face persona={p} size={30} />
                  <span className="hand">{p.persona_name || p.name}</span>
                  <span className="persona-tag__mini">{p.running ? '醒着' : '睡着'}</span>
                </button>
              </li>
            ))}
          </motion.ul>
        )}
      </AnimatePresence>
    </div>
  );
}
