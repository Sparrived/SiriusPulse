import { AnimatePresence, motion } from 'motion/react';
import { useState } from 'react';
import { useCurrentPersona } from '@/lib/queries';
import { Segmented } from '@/ui/form/fields';
import { History } from './History';
import { McpPanel } from './McpPanel';
import { PluginsShelf } from './PluginsShelf';
import { ToolsShelf } from './ToolsShelf';
import './bag.css';

type Tab = 'tools' | 'plugins' | 'mcp' | 'history';

/** 背包：她会的本领——自己会用的小工具、大家召唤的插件、请来的外援，以及用过的记录 */
export function Bag() {
  const persona = useCurrentPersona();
  const [tab, setTab] = useState<Tab>('tools');
  if (!persona) return null;

  return (
    <motion.div
      className="room-column room-column--wide bag"
      initial={{ opacity: 0, y: 30 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: 20 }}
      transition={{ type: 'spring', stiffness: 120, damping: 20 }}
    >
      <div className="room-title">
        <div>
          <h1 className="hand">背包</h1>
          <p>{persona.persona_name || persona.name}会的本领：小工具、插件和外援。</p>
        </div>
        <Segmented
          value={tab}
          onChange={setTab}
          options={[
            { value: 'tools', label: '小工具' },
            { value: 'plugins', label: '插件' },
            { value: 'mcp', label: '外援' },
            { value: 'history', label: '用过的' },
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
          {tab === 'tools' && <ToolsShelf persona={persona.name} />}
          {tab === 'plugins' && <PluginsShelf />}
          {tab === 'mcp' && <McpPanel persona={persona.name} />}
          {tab === 'history' && <History />}
        </motion.div>
      </AnimatePresence>
    </motion.div>
  );
}
