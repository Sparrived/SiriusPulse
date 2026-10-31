import { AnimatePresence, motion } from 'motion/react';
import { useEffect } from 'react';
import { useLive, type ThoughtKind } from '@/state/live';
import { useScene } from '@/state/scene';
import './thoughts.css';

const GLYPH: Record<ThoughtKind, string> = {
  hear: '♪',
  feel: '♡',
  think: '…',
  tool: '✦',
  reply: '✉',
  quiet: '·',
  self: '☾',
  remind: '◷',
};

const LIFETIME = 6500;

/** 她头顶冒出来的小念头。来自引擎的实时事件，也来自你摸她头时她的回应。 */
export function Thoughts() {
  const thoughts = useLive((s) => s.thoughts);
  const forget = useLive((s) => s.forget);
  const mode = useScene((s) => s.mode);

  useEffect(() => {
    if (!thoughts.length) return;
    const oldest = thoughts[0];
    const wait = Math.max(400, LIFETIME - (Date.now() - oldest.at));
    const timer = window.setTimeout(() => forget(oldest.id), wait);
    return () => window.clearTimeout(timer);
  }, [thoughts, forget]);

  if (mode === 'door' || mode === 'sky') return null;

  return (
    <div className={`thoughts thoughts--${mode}`} aria-live="polite">
      <AnimatePresence initial={false}>
        {thoughts.map((t, i) => (
          <motion.div
            key={t.id}
            layout
            className={`thought thought--${t.kind}`}
            initial={{ opacity: 0, y: 14, scale: 0.8, rotate: -3 }}
            animate={{ opacity: 1 - (thoughts.length - 1 - i) * 0.18, y: 0, scale: 1, rotate: 0 }}
            exit={{ opacity: 0, y: -18, scale: 0.9, filter: 'blur(4px)' }}
            transition={{ type: 'spring', stiffness: 260, damping: 22 }}
          >
            <span className="thought__glyph" aria-hidden>
              {GLYPH[t.kind]}
            </span>
            <span className="hand">{t.text}</span>
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  );
}
