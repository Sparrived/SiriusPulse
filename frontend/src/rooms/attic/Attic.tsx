import { AnimatePresence, motion } from 'motion/react';
import { useState } from 'react';
import { useCurrentPersona } from '@/lib/queries';
import { Segmented } from '@/ui/form/fields';
import { Amkr } from './Amkr';
import { Logs } from './Logs';
import { Settings } from './Settings';
import { Upkeep } from './Upkeep';
import './attic.css';

type Tab = 'settings' | 'amkr' | 'logs' | 'upkeep';

/** 阁楼：整座房子的维护——全局设置、AMKR、日志、熄灯。这里的东西所有伙伴共用。 */
export function Attic() {
  const persona = useCurrentPersona();
  const [tab, setTab] = useState<Tab>('settings');
  if (!persona) return null;

  return (
    <motion.div
      className="room-column room-column--wide attic"
      initial={{ opacity: 0, y: 30 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: 20 }}
      transition={{ type: 'spring', stiffness: 120, damping: 20 }}
    >
      <div className="room-title">
        <div>
          <h1 className="hand">阁楼</h1>
          <p>整座房子的维护：门牌、厨房（AMKR）、监控录像和电闸。所有伙伴共用。</p>
        </div>
        <Segmented
          value={tab}
          onChange={setTab}
          options={[
            { value: 'settings', label: '设置' },
            { value: 'amkr', label: 'AMKR' },
            { value: 'logs', label: '日志' },
            { value: 'upkeep', label: '维护' },
          ]}
        />
      </div>
      <AnimatePresence mode="wait">
        <motion.div
          key={tab}
          className="bag__pane"
          initial={{ opacity: 0, y: 14 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -10 }}
          transition={{ duration: 0.25 }}
        >
          {tab === 'settings' && <Settings />}
          {tab === 'amkr' && <Amkr />}
          {tab === 'logs' && <Logs persona={persona.name} />}
          {tab === 'upkeep' && <Upkeep />}
        </motion.div>
      </AnimatePresence>
    </motion.div>
  );
}
