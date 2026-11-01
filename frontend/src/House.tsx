import { AnimatePresence } from 'motion/react';
import { useEffect, type ComponentType } from 'react';
import { useLocation } from 'wouter';
import {
  RESOURCE_QUERIES,
  queryClient,
  useAvatar,
  useCurrentPersona,
  useGlance,
  usePersonas,
} from '@/lib/queries';
import { eventToThought } from '@/lib/thoughts';
import { onEngineEvent, startEvents } from '@/lib/ws';
import { roomByPath } from '@/rooms';
import { Attic } from '@/rooms/attic/Attic';
import { Bag } from '@/rooms/bag/Bag';
import { Diary } from '@/rooms/diary/Diary';
import { Heart } from '@/rooms/heart/Heart';
import { Home } from '@/rooms/Home';
import { Ledger } from '@/rooms/ledger/Ledger';
import { Mailbox } from '@/rooms/mailbox/Mailbox';
import { Memories } from '@/rooms/memories/Memories';
import { Mirror } from '@/rooms/mirror/Mirror';
import { Stars } from '@/rooms/stars/Stars';
import { useLive } from '@/state/live';
import { usePersonaStore } from '@/state/persona';
import { useScene } from '@/state/scene';
import { CommandPalette } from '@/ui/CommandPalette';
import { Dock } from '@/ui/Dock';
import { PersonaTag } from '@/ui/PersonaTag';
import { Thoughts } from '@/ui/Thoughts';

/** 没选过伙伴时，挑一个最可能是「她」的 */
function useEnsurePersona() {
  const { data } = usePersonas();
  const current = usePersonaStore((s) => s.current);
  const choose = usePersonaStore((s) => s.choose);
  useEffect(() => {
    if (!data?.personas.length) return;
    if (current && data.personas.some((p) => p.name === current)) return;
    const pick =
      data.personas.find((p) => p.name === data.active) ??
      data.personas.find((p) => p.running) ??
      data.personas.find((p) => p.name === 'sirius') ??
      data.personas[0];
    choose(pick.name);
  }, [data, current, choose]);
}

/** 引擎事件 → 查询失效 + 她的小念头 */
function useEngineEvents() {
  useEffect(() => {
    const stop = startEvents();
    const off = onEngineEvent((event) => {
      if (event.type === 'data_changed') {
        for (const resource of event.resources ?? []) {
          for (const queryKey of RESOURCE_QUERIES[resource] ?? []) {
            queryClient.invalidateQueries({ queryKey });
          }
        }
        return;
      }
      if (event.persona && event.persona !== usePersonaStore.getState().current) return;
      const thought = eventToThought(event);
      if (thought) useLive.getState().think(...thought);
    });
    return () => {
      off();
      stop();
    };
  }, []);
}

/** 把数据同步给 3D 世界：她醒着没、心情如何、长什么样、镜头该在哪 */
function useSceneSync(path: string) {
  const persona = useCurrentPersona();
  const { data: glance } = useGlance();
  const { data: avatar } = useAvatar(persona?.name ?? null);
  const set = useScene((s) => s.set);

  useEffect(() => {
    set({ mode: roomByPath(path)?.scene ?? 'room' });
  }, [path, set]);

  useEffect(() => {
    if (persona) set({ sleeping: !persona.running });
  }, [persona, set]);

  useEffect(() => {
    if (glance?.mood) set({ valence: glance.mood.valence, arousal: glance.mood.arousal });
  }, [glance, set]);

  useEffect(() => {
    if (avatar) set({ avatarUrl: avatar.url });
  }, [avatar, set]);

  // 换了伙伴，旧的念头就不要再挂着了
  useEffect(() => useLive.getState().clear(), [persona?.name]);
}

/** 已经搬进新家的房间 */
const FURNISHED: Record<string, ComponentType> = {
  home: Home,
  mirror: Mirror,
  stars: Stars,
  mailbox: Mailbox,
  diary: Diary,
  memories: Memories,
  heart: Heart,
  bag: Bag,
  ledger: Ledger,
  attic: Attic,
};

export function House() {
  const [location] = useLocation();
  useEnsurePersona();
  useEngineEvents();
  useSceneSync(location);
  const room = roomByPath(location);

  return (
    <>
      <PersonaTag />
      <CommandPalette />
      <Thoughts />
      <AnimatePresence mode="wait">
        {(() => {
          const id = room && FURNISHED[room.id] ? room.id : 'home';
          const Room = FURNISHED[id];
          return <Room key={id} />;
        })()}
      </AnimatePresence>
      <Dock />
    </>
  );
}
