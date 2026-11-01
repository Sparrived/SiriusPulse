import { AnimatePresence, motion } from 'motion/react';
import { useState } from 'react';
import { useCurrentPersona } from '@/lib/queries';
import { Segmented } from '@/ui/form/fields';
import { AvatarTab } from './AvatarTab';
import { PersonalityTab } from './PersonalityTab';
import { ProfileTab } from './ProfileTab';
import { RhythmTab } from './RhythmTab';
import './mirror.css';

type Tab = 'who' | 'temper' | 'rhythm' | 'look';

const TABS: { value: Tab; label: string }[] = [
  { value: 'who', label: '她是谁' },
  { value: 'temper', label: '性格' },
  { value: 'rhythm', label: '一天的节奏' },
  { value: 'look', label: '立绘' },
];

/** 镜子：她站在左边，你在镜子里替她梳妆——名字、性子、作息、样子。 */
export function Mirror() {
  const persona = useCurrentPersona();
  const [tab, setTab] = useState<Tab>('who');
  if (!persona) return null;
  const name = persona.persona_name || persona.name;

  return (
    <motion.div
      className="room-column mirror"
      initial={{ opacity: 0, x: 40 }}
      animate={{ opacity: 1, x: 0 }}
      exit={{ opacity: 0, x: 30 }}
      transition={{ type: 'spring', stiffness: 120, damping: 20 }}
    >
      <div className="room-title">
        <div>
          <h1 className="hand">镜子</h1>
          <p>替{name}照照镜子：名字、性子、作息和样子都在这里。</p>
        </div>
      </div>
      <div className="mirror__tabs">
        <Segmented value={tab} options={TABS} onChange={setTab} />
      </div>
      <AnimatePresence mode="wait">
        <motion.div
          key={`${tab}-${persona.name}`}
          className="mirror__pane"
          initial={{ opacity: 0, y: 14 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -10 }}
          transition={{ duration: 0.28, ease: [0.22, 1, 0.36, 1] }}
        >
          {tab === 'who' && <ProfileTab persona={persona.name} />}
          {tab === 'temper' && <PersonalityTab persona={persona.name} />}
          {tab === 'rhythm' && <RhythmTab persona={persona.name} />}
          {tab === 'look' && <AvatarTab persona={persona.name} name={name} />}
        </motion.div>
      </AnimatePresence>
    </motion.div>
  );
}
