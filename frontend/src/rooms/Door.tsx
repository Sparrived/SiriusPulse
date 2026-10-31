import { AnimatePresence, motion, useAnimationControls } from 'motion/react';
import { useState } from 'react';
import { knock } from '@/lib/auth';
import './door.css';

/** 敲门：登录不是填表，是回家。 */
export function Door({ onOpen }: { onOpen: () => void }) {
  const [secret, setSecret] = useState('');
  const [busy, setBusy] = useState(false);
  const [reply, setReply] = useState<string | null>(null);
  const [knocks, setKnocks] = useState(0);
  const [opening, setOpening] = useState(false);
  const card = useAnimationControls();

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!secret || busy) return;
    setBusy(true);
    setReply(null);
    // 咚、咚、咚
    for (let i = 1; i <= 3; i++) {
      setKnocks(i);
      await card.start({ rotate: [0, -1.2, 0.8, 0], transition: { duration: 0.22 } });
    }
    const result = await knock(secret);
    setKnocks(0);
    setBusy(false);
    if (result.ok) {
      setOpening(true);
      window.setTimeout(onOpen, 900);
      return;
    }
    setReply(result.reason);
    card.start({ x: [0, -10, 9, -6, 4, 0], transition: { duration: 0.45 } });
  };

  return (
    <div className="door">
      <motion.div
        initial={{ opacity: 0, y: 24 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.9, delay: 0.3, ease: [0.22, 1, 0.36, 1] }}
      >
        <motion.form className="door__card paper" onSubmit={submit} animate={card}>
          <div className="door__lamp" aria-hidden />
          <p className="door__eyebrow">Sirius Pulse</p>
          <h1 className="door__title hand">是你回来了吗？</h1>
          <p className="door__sub">屋里的灯还亮着。说出暗号，她就来开门。</p>

          <label className="door__field">
            <span className="visually-hidden">暗号</span>
            <input
              type="password"
              autoFocus
              autoComplete="current-password"
              placeholder="暗号"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              disabled={busy || opening}
            />
          </label>

          <button className="soft-btn door__knock" disabled={busy || opening || !secret}>
            {busy ? '咚咚咚……' : '敲敲门'}
          </button>

          <div className="door__reply" aria-live="polite">
            <AnimatePresence mode="wait">
              {knocks > 0 && (
                <motion.span
                  key={`k${knocks}`}
                  className="hand"
                  initial={{ opacity: 0, scale: 0.6 }}
                  animate={{ opacity: 1, scale: 1 }}
                  exit={{ opacity: 0 }}
                >
                  {'咚'.repeat(knocks)}
                </motion.span>
              )}
              {reply && knocks === 0 && (
                <motion.span
                  key="reply"
                  className="hand door__nope"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                >
                  {reply}
                </motion.span>
              )}
            </AnimatePresence>
          </div>

          <p className="door__help">忘了暗号？它在第一次启动时打印在 WebUI 日志里（用户名 admin）。</p>
        </motion.form>
      </motion.div>

      <AnimatePresence>
        {opening && (
          <motion.div
            className="door__light"
            initial={{ opacity: 0, scale: 0.2 }}
            animate={{ opacity: 1, scale: 3 }}
            transition={{ duration: 0.95, ease: [0.7, 0, 0.3, 1] }}
          />
        )}
      </AnimatePresence>
    </div>
  );
}
