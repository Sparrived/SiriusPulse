import { motion } from 'motion/react';
import { useMemo } from 'react';
import {
  queryClient,
  saveAdapters,
  saveExperience,
  useAdapters,
  useCurrentPersona,
  useExperience,
  useHealth,
} from '@/lib/queries';
import type { NapCatAdapter } from '@/lib/types';
import { SaveWhisper } from '@/ui/form/fields';
import { useAutosave, useDraft } from '@/ui/form/useDraft';
import { Accounts } from './Accounts';
import { AdapterCard, NEW_ADAPTER, type Strategy } from './AdapterCard';
import { DispatchBoard } from './DispatchBoard';
import './mailbox.css';

type Strategies = { group_reply_strategies: Record<string, Strategy> };

/** 信箱：她和外面世界的联系——QQ、群、私聊、她自己的账号，以及伙伴们在群里怎么排班。 */
export function Mailbox() {
  const persona = useCurrentPersona();
  const name = persona?.name ?? null;
  const adapters = useAdapters();
  const experience = useExperience();
  const { data: health } = useHealth();

  const adapterDraft = useDraft<{ adapters: NapCatAdapter[] }>(
    adapters.data,
    name,
    adapters.isFetchedAfterMount,
  );
  const adapterSave = useAutosave(
    adapterDraft.draft,
    adapterDraft.baseline,
    adapterDraft.setBaseline,
    async (v) => {
      await saveAdapters(name!, v.adapters);
      queryClient.invalidateQueries({ queryKey: ['adapters', name] });
    },
  );

  // 群级说话习惯存在 experience 里；这里只提交这一个字段，不碰镜子里的那些
  const strategyServer = useMemo<Strategies | undefined>(
    () =>
      experience.data ? { group_reply_strategies: experience.data.group_reply_strategies ?? {} } : undefined,
    [experience.data],
  );
  const strategyDraft = useDraft<Strategies>(strategyServer, name, experience.isFetchedAfterMount);
  const strategySave = useAutosave(
    strategyDraft.draft,
    strategyDraft.baseline,
    strategyDraft.setBaseline,
    async (v) => {
      await saveExperience(name!, v);
      queryClient.invalidateQueries({ queryKey: ['experience', name] });
    },
  );

  if (!persona) return null;
  const list = adapterDraft.draft?.adapters ?? [];
  const setList = (next: NapCatAdapter[]) => adapterDraft.setDraft({ adapters: next });
  const strategies = strategyDraft.draft?.group_reply_strategies ?? {};

  const adapterCheck = health?.checks?.adapter;
  const stamp: 'ok' | 'weak' | 'down' | 'sleep' = !persona.running
    ? 'sleep'
    : typeof adapterCheck === 'object'
      ? adapterCheck.online
        ? adapterCheck.degraded
          ? 'weak'
          : 'ok'
        : 'down'
      : adapterCheck
        ? 'ok'
        : 'down';

  // 两份草稿里谁的状态更「要紧」就显示谁
  const order = ['error', 'saving', 'pending', 'saved', 'idle'] as const;
  const status = [adapterSave, strategySave].sort(
    (a, b) => order.indexOf(a.status) - order.indexOf(b.status),
  )[0];

  return (
    <motion.div
      className="room-column room-column--wide mailbox"
      initial={{ opacity: 0, y: 30 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: 20 }}
      transition={{ type: 'spring', stiffness: 120, damping: 20 }}
    >
      <div className="room-title">
        <div>
          <h1 className="hand">信箱</h1>
          <p>{persona.persona_name || persona.name}和外面世界的联系：用哪个 QQ、去哪些群、谁能找她。</p>
        </div>
        <SaveWhisper {...status} />
      </div>

      {adapters.isError && <p className="mirror__error">{(adapters.error as Error).message}</p>}
      {!adapterDraft.draft && !adapters.isError && <p className="mirror__loading hand">正在翻信箱……</p>}

      {list.map((adapter, i) => (
        <AdapterCard
          key={i}
          index={i}
          adapter={adapter}
          stamp={stamp}
          onChange={(a) => setList(list.map((x, j) => (j === i ? a : x)))}
          onRemove={list.length > 1 ? () => setList(list.filter((_, j) => j !== i)) : undefined}
          strategies={strategies}
          onStrategies={(group_reply_strategies) => strategyDraft.setDraft({ group_reply_strategies })}
        />
      ))}
      {adapterDraft.draft && (
        <button
          className="soft-btn ghost mailbox__add"
          onClick={() => setList([...list, { ...NEW_ADAPTER, enabled: !list.length }])}
        >
          + {list.length ? '再接一个 QQ' : '给她接上 QQ'}
        </button>
      )}

      <Accounts persona={persona.name} />
      <DispatchBoard />
    </motion.div>
  );
}
