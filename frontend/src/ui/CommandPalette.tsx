import { AnimatePresence, motion } from 'motion/react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useLocation } from 'wouter';
import { setToken } from '@/lib/auth';
import { useCurrentPersona, usePersonas, useWakeSleep } from '@/lib/queries';
import { ROOMS } from '@/rooms';
import { useLive } from '@/state/live';
import { usePersonaStore } from '@/state/persona';
import { usePrefs } from '@/state/prefs';
import './palette.css';

interface Command {
  id: string;
  group: string;
  label: string;
  hint?: string;
  run: () => void;
}

const isMac = /Mac|iPhone|iPad/.test(navigator.platform);

/** ⌘K / Ctrl+K：「想去哪儿？」——想做的事直接说，不用记它在哪个菜单里。 */
export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [cursor, setCursor] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const [, navigate] = useLocation();
  const persona = useCurrentPersona();
  const { data } = usePersonas();
  const choose = usePersonaStore((s) => s.choose);
  const prefs = usePrefs();
  const wakeSleep = useWakeSleep();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setOpen((v) => !v);
      }
    };
    const onOpen = () => setOpen(true);
    window.addEventListener('keydown', onKey);
    window.addEventListener('sirius:palette', onOpen);
    return () => {
      window.removeEventListener('keydown', onKey);
      window.removeEventListener('sirius:palette', onOpen);
    };
  }, []);

  useEffect(() => {
    if (open) {
      setQuery('');
      setCursor(0);
      requestAnimationFrame(() => input.current?.focus());
    }
  }, [open]);

  const commands = useMemo<Command[]>(() => {
    const list: Command[] = ROOMS.map((room) => ({
      id: `room:${room.id}`,
      group: '去房间',
      label: room.name,
      hint: room.hint,
      run: () => navigate(room.path),
    }));
    if (persona) {
      const name = persona.persona_name || persona.name;
      list.push({
        id: 'wake',
        group: '和她',
        label: persona.running ? `哄${name}睡觉` : `叫醒${name}`,
        hint: persona.running ? '停止这个人格' : '启动这个人格',
        run: () => {
          wakeSleep.mutate({ name: persona.name, wake: !persona.running });
          useLive.getState().think('feel', persona.running ? '那我先睡啦，晚安～' : '唔……我醒啦！');
        },
      });
    }
    for (const p of data?.personas ?? []) {
      if (p.name === persona?.name) continue;
      list.push({
        id: `visit:${p.name}`,
        group: '串门',
        label: `去${p.persona_name || p.name}家`,
        hint: p.running ? '醒着' : '睡着了',
        run: () => choose(p.name),
      });
    }
    list.push(
      {
        id: 'calm',
        group: '偏好',
        label: prefs.calm ? '关闭安静模式' : '安静模式',
        hint: '减少摇摆、视差与转场',
        run: () => prefs.toggle('calm'),
      },
      {
        id: 'low',
        group: '偏好',
        label: prefs.lowPower ? '关闭省电模式' : '省电模式',
        hint: '少一些星星，关掉光晕',
        run: () => prefs.toggle('lowPower'),
      },
      {
        id: 'legacy',
        group: '其它',
        label: '去旧版控制台',
        hint: '搬家期间保留，功能齐全',
        run: () => window.open('/legacy/', '_blank'),
      },
      {
        id: 'leave',
        group: '其它',
        label: '出门（退出登录）',
        run: () => setToken(null),
      },
    );
    return list;
  }, [navigate, persona, data, choose, prefs, wakeSleep]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return commands;
    return commands.filter((c) => `${c.label} ${c.hint ?? ''} ${c.group}`.toLowerCase().includes(q));
  }, [commands, query]);

  const run = (cmd: Command | undefined) => {
    if (!cmd) return;
    setOpen(false);
    cmd.run();
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setCursor((c) => Math.min(filtered.length - 1, c + 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setCursor((c) => Math.max(0, c - 1));
    } else if (e.key === 'Enter') {
      run(filtered[cursor]);
    } else if (e.key === 'Escape') {
      setOpen(false);
    }
  };

  let lastGroup = '';
  return (
    <>
      <button className="palette-trigger paper" onClick={() => setOpen(true)}>
        <span className="hand">想去哪儿？</span>
        <kbd>{isMac ? '⌘' : 'Ctrl'} K</kbd>
      </button>
      <AnimatePresence>
        {open && (
          <motion.div
            className="palette-veil"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onPointerDown={(e) => e.target === e.currentTarget && setOpen(false)}
          >
            <motion.div
              className="palette paper"
              role="dialog"
              aria-label="想去哪儿"
              initial={{ opacity: 0, y: 20, scale: 0.97 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 10, scale: 0.98 }}
              transition={{ type: 'spring', stiffness: 380, damping: 32 }}
            >
              <input
                ref={input}
                className="palette__input hand"
                placeholder="想去哪儿，想做什么？"
                value={query}
                onChange={(e) => {
                  setQuery(e.target.value);
                  setCursor(0);
                }}
                onKeyDown={onKeyDown}
              />
              <ul className="palette__list">
                {filtered.length === 0 && <li className="palette__empty hand">唔，我没听懂……换个说法？</li>}
                {filtered.map((cmd, i) => {
                  const header = cmd.group !== lastGroup ? cmd.group : null;
                  lastGroup = cmd.group;
                  return (
                    <li key={cmd.id}>
                      {header && <div className="palette__group">{header}</div>}
                      <button
                        className={`palette__item${i === cursor ? ' is-on' : ''}`}
                        onMouseEnter={() => setCursor(i)}
                        onClick={() => run(cmd)}
                      >
                        <span className="hand">{cmd.label}</span>
                        {cmd.hint && <span className="palette__hint">{cmd.hint}</span>}
                      </button>
                    </li>
                  );
                })}
              </ul>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </>
  );
}
