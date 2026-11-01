import { AnimatePresence, motion } from 'motion/react';
import { useEffect, useState } from 'react';
import { usePersonas } from '@/lib/queries';
import type { PersonaSummary } from '@/lib/types';
import { NEW_STAR, starAnchors, useScene } from '@/state/scene';
import { Face } from '@/ui/PersonaTag';
import { Ritual } from './Ritual';
import { StarCard } from './StarCard';
import './stars.css';

function Label({
  name,
  children,
  onClick,
  focused,
  className,
}: {
  name: string;
  children: React.ReactNode;
  onClick: () => void;
  focused: boolean;
  className?: string;
}) {
  return (
    <button
      ref={(el) => {
        if (el) starAnchors.set(name, el);
        else starAnchors.delete(name);
      }}
      className={`star-label${focused ? ' is-focused' : ''} ${className ?? ''}`}
      onClick={onClick}
      onPointerEnter={() => useScene.getState().set({ focusStar: name })}
      onPointerLeave={() =>
        useScene
          .getState()
          .set({ focusStar: useScene.getState().focusStar === name ? null : useScene.getState().focusStar })
      }
    >
      {children}
    </button>
  );
}

/** 星图：抬头看，伙伴们都是天上的星星。 */
export function Stars() {
  const { data } = usePersonas();
  const setScene = useScene((s) => s.set);
  const [picked, setPicked] = useState<string | null>(null);
  const [welcoming, setWelcoming] = useState(false);
  const personas = data?.personas ?? [];

  useEffect(() => {
    setScene({ stars: personas.map((p) => ({ name: p.name, awake: p.running })) });
  }, [data, setScene]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    setScene({ focusStar: picked });
  }, [picked, setScene]);

  useEffect(() => () => setScene({ focusStar: null }), [setScene]);

  const pickedPersona: PersonaSummary | undefined = personas.find((p) => p.name === picked);
  const awake = personas.filter((p) => p.running).length;

  return (
    <motion.div className="stars" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
      <div className="stars__title">
        <h1 className="hand">星图</h1>
        <p>
          天上一共 <b className="num">{personas.length}</b> 颗星，<b className="num">{awake}</b>{' '}
          颗醒着。点一颗星去看看她。
        </p>
      </div>

      {personas.map((p) => (
        <Label
          key={p.name}
          name={p.name}
          focused={picked === p.name}
          onClick={() => setPicked(picked === p.name ? null : p.name)}
        >
          <Face persona={p} size={30} />
          <span className="hand">{p.persona_name || p.name}</span>
        </Label>
      ))}
      <Label
        name={NEW_STAR}
        focused={welcoming}
        className="star-label--new"
        onClick={() => setWelcoming(true)}
      >
        <span className="star-label__plus">+</span>
        <span className="hand">迎接新伙伴</span>
      </Label>

      <AnimatePresence>
        {pickedPersona && !welcoming && (
          <StarCard key={pickedPersona.name} persona={pickedPersona} onClose={() => setPicked(null)} />
        )}
      </AnimatePresence>
      <AnimatePresence>
        {welcoming && (
          <Ritual
            taken={personas.map((p) => p.name)}
            onClose={(created) => {
              setWelcoming(false);
              if (created) setPicked(created);
            }}
          />
        )}
      </AnimatePresence>
    </motion.div>
  );
}
