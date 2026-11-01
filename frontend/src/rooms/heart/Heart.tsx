import { AnimatePresence, motion } from 'motion/react';
import { useState } from 'react';
import { useCurrentPersona } from '@/lib/queries';
import { Segmented } from '@/ui/form/fields';
import { Weather } from './Weather';
import { Wishes } from './Wishes';
import './heart.css';

type Tab = 'weather' | 'wishes';

/** 心事：她怎么感受身边的人，以及她自己心里挂着的事 */
export function Heart() {
  const persona = useCurrentPersona();
  const [tab, setTab] = useState<Tab>('weather');
  if (!persona) return null;
  const name = persona.persona_name || persona.name;

  return (
    <motion.div
      className="room-column room-column--wide heart"
      initial={{ opacity: 0, y: 30 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: 20 }}
      transition={{ type: 'spring', stiffness: 120, damping: 20 }}
    >
      <div className="room-title">
        <div>
          <h1 className="hand">心事</h1>
          <p>{name}心里的天气，和她惦记着的事。</p>
        </div>
        <Segmented
          value={tab}
          onChange={setTab}
          options={[
            { value: 'weather', label: '情绪天气' },
            { value: 'wishes', label: '心愿便签' },
          ]}
        />
      </div>
      <AnimatePresence mode="wait">
        <motion.div
          key={tab}
          className="heart__pane"
          initial={{ opacity: 0, y: 14 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -10 }}
          transition={{ duration: 0.25 }}
        >
          {tab === 'weather' ? <Weather name={name} /> : <Wishes name={name} />}
        </motion.div>
      </AnimatePresence>
    </motion.div>
  );
}
