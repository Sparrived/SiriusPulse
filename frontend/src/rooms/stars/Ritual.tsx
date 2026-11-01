import { AnimatePresence, motion } from 'motion/react';
import { useMemo, useState } from 'react';
import { useLocation } from 'wouter';
import { api } from '@/lib/api';
import { queryClient, saveProfile } from '@/lib/queries';
import { useLive } from '@/state/live';
import { usePersonaStore } from '@/state/persona';
import { Chips, Field, Letter, TextInput } from '@/ui/form/fields';

// 与后端 api_persona_create 的校验保持一致
const FOLDER_RE = /^[a-zA-Z0-9_\-一-鿿]+$/;

type Step = 0 | 1 | 2 | 3;

/**
 * 迎接新伙伴：起名字 → 说说她是什么样的人（可以先空着）→ 点亮她的星星。
 * 档案直接写进新伙伴自己的家（显式指定人格），不会误写进正在看的那位。
 */
export function Ritual({ taken, onClose }: { taken: string[]; onClose: (created?: string) => void }) {
  const [step, setStep] = useState<Step>(0);
  const [display, setDisplay] = useState('');
  const [folder, setFolder] = useState('');
  const [folderTouched, setFolderTouched] = useState(false);
  const [aliases, setAliases] = useState<string[]>([]);
  const [letter, setLetter] = useState('');
  const [avatar, setAvatar] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [, navigate] = useLocation();
  const choose = usePersonaStore((s) => s.choose);

  const id = folderTouched ? folder : display.trim();
  const idProblem = useMemo(() => {
    if (!id) return '门牌号不能空着';
    if (!FOLDER_RE.test(id)) return '只能用字母、数字、中文、下划线和连字符';
    if (taken.includes(id)) return '这个门牌号已经有人住了';
    return null;
  }, [id, taken]);

  const light = async () => {
    setBusy(true);
    setProblem(null);
    try {
      await api.post('/api/personas', { name: id, display_name: display.trim() }, { global: true });
      if (aliases.length || letter.trim()) {
        await saveProfile(id, { name: display.trim(), aliases, full_system_prompt: letter });
      }
      if (avatar) {
        await api.post(`/api/personas/${encodeURIComponent(id)}/avatar`, avatar, {
          global: true,
          headers: { 'Content-Type': avatar.type || 'application/octet-stream' },
        });
      }
      await queryClient.invalidateQueries({ queryKey: ['personas'] });
      useLive.getState().think('self', `天上多了一颗星：${display.trim()}`);
      setStep(3);
    } catch (e) {
      setProblem((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const pages = [
    <div key="0">
      <p className="ritual__kicker">第一步</p>
      <h2 className="hand">给她起个名字吧</h2>
      <input
        className="ritual__name hand"
        autoFocus
        value={display}
        placeholder="她叫……"
        onChange={(e) => setDisplay(e.target.value)}
        onKeyDown={(e) => e.key === 'Enter' && display.trim() && !idProblem && setStep(1)}
      />
      <Field label="门牌号" hint={idProblem ?? '她在 data/personas 下的文件夹名，之后不能改'}>
        <TextInput
          value={id}
          mono
          onChange={(v) => {
            setFolderTouched(true);
            setFolder(v.trim());
          }}
        />
      </Field>
      <div className="ritual__nav">
        <button className="soft-btn ghost" onClick={() => onClose()}>
          改天再说
        </button>
        <button className="soft-btn" disabled={!display.trim() || !!idProblem} onClick={() => setStep(1)}>
          下一步
        </button>
      </div>
    </div>,
    <div key="1">
      <p className="ritual__kicker">第二步 · 可以先空着，之后在镜子里慢慢写</p>
      <h2 className="hand">{display.trim()}是个什么样的人？</h2>
      <Field label="大家还会怎么叫她">
        <Chips values={aliases} onChange={setAliases} placeholder="昵称，回车添加" />
      </Field>
      <Field label="写给她的第一封信">
        <Letter
          value={letter}
          onChange={setLetter}
          placeholder={`亲爱的${display.trim()}：\n你是……`}
          minRows={6}
        />
      </Field>
      <Field label="她的样子" hint="背景透明的 PNG 最好；不选就先借用月白的样子">
        <input
          type="file"
          accept="image/png,image/webp,image/jpeg"
          onChange={(e) => setAvatar(e.target.files?.[0] ?? null)}
          className="ritual__file"
        />
      </Field>
      <div className="ritual__nav">
        <button className="soft-btn ghost" onClick={() => setStep(0)}>
          上一步
        </button>
        <button className="soft-btn" onClick={() => setStep(2)}>
          下一步
        </button>
      </div>
    </div>,
    <div key="2" className="ritual__light">
      <p className="ritual__kicker">最后一步</p>
      <h2 className="hand">点亮{display.trim()}的星星</h2>
      <p className="ritual__summary">
        她会住进 <code>data/personas/{id}</code>。
        {aliases.length > 0 && <> 大家可以叫她「{aliases.join('」「')}」。</>}
        {letter.trim() ? ' 信已经写好了。' : ' 信还空着。'}
      </p>
      {problem && <p className="star-card__error">{problem}</p>}
      <div className="ritual__nav">
        <button className="soft-btn ghost" disabled={busy} onClick={() => setStep(1)}>
          上一步
        </button>
        <button className="soft-btn blush ritual__go" disabled={busy} onClick={light}>
          {busy ? '星星正在亮起来……' : '✦ 点亮她'}
        </button>
      </div>
    </div>,
    <div key="3" className="ritual__done">
      <h2 className="hand">欢迎你，{display.trim()}</h2>
      <p>
        她的星星亮了。不过要真正开口说话，她还需要：在阁楼里为她登记 AMKR 任务（搬好之前在
        <a href="/legacy/#amkr" target="_blank" rel="noreferrer">
          旧版的 AMKR 运维
        </a>
        ），再去信箱接上她的 QQ。
      </p>
      <div className="ritual__nav">
        <button className="soft-btn ghost" onClick={() => onClose(id)}>
          留在星图
        </button>
        <button
          className="soft-btn"
          onClick={() => {
            choose(id);
            navigate('/mailbox');
          }}
        >
          去她家接上 QQ
        </button>
      </div>
    </div>,
  ];

  return (
    <motion.div
      className="ritual-veil"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
    >
      <motion.section
        className="ritual paper"
        role="dialog"
        aria-label="迎接新伙伴"
        initial={{ opacity: 0, y: 40, scale: 0.95 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: 30 }}
        transition={{ type: 'spring', stiffness: 200, damping: 24 }}
      >
        <div className="ritual__dots" aria-hidden>
          {[0, 1, 2].map((i) => (
            <span key={i} className={i <= step ? 'is-on' : ''} />
          ))}
        </div>
        <AnimatePresence mode="wait" initial={false}>
          <motion.div
            key={step}
            initial={{ opacity: 0, x: 30 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: -30 }}
            transition={{ duration: 0.3, ease: [0.22, 1, 0.36, 1] }}
          >
            {pages[step]}
          </motion.div>
        </AnimatePresence>
      </motion.section>
    </motion.div>
  );
}
