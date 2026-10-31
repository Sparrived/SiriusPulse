import { motion, type Variants } from 'motion/react';
import { Link } from 'wouter';
import { EMOTION_CN, moodLine } from '@/lib/mood';
import { useCurrentPersona, useGlance, useHealth, useMetrics, useWakeSleep } from '@/lib/queries';
import { ago, duration, greeting } from '@/lib/time';
import { useLive } from '@/state/live';
import { CountUp, MoodCompass } from '@/ui/bits';
import { MoonIcon, SunIcon } from '@/ui/icons';
import './home.css';

const column: Variants = {
  hidden: {},
  show: { transition: { staggerChildren: 0.09, delayChildren: 0.25 } },
};
const card: Variants = {
  hidden: { opacity: 0, y: 26, rotate: 0.6 },
  show: { opacity: 1, y: 0, rotate: 0, transition: { type: 'spring', stiffness: 120, damping: 18 } },
};

function groupName(id: string) {
  return id.length > 6 ? `群 …${id.slice(-4)}` : `群 ${id}`;
}

/** 客厅：推门进来，她就在这里。 */
export function Home() {
  const persona = useCurrentPersona();
  const { data: glance } = useGlance();
  const { data: metrics } = useMetrics();
  const { data: health } = useHealth();
  const wakeSleep = useWakeSleep();
  const think = useLive((s) => s.think);

  if (!persona) return null;
  const name = persona.persona_name || persona.name;
  const mood = glance?.mood;
  const convo = glance?.conversation;
  const adapter = health?.checks?.adapter;
  const adapterOffline =
    persona.running && adapter && typeof adapter === 'object' && adapter.online === false;

  const toggle = () => {
    const wake = !persona.running;
    wakeSleep.mutate({ name: persona.name, wake });
    think('feel', wake ? '唔……我醒啦！' : '那我先睡啦，晚安～');
  };

  return (
    <motion.section className="home" variants={column} initial="hidden" animate="show" exit="hidden">
      <motion.article className="home__hello paper" variants={card}>
        <p className="home__greet hand">{greeting()}</p>
        <div className="home__mood">
          {mood && (
            <MoodCompass
              valence={mood.valence}
              arousal={mood.arousal}
              baseValence={mood.baseline_valence}
              baseArousal={mood.baseline_arousal}
            />
          )}
          <div>
            <p className="home__feeling hand">
              {persona.running
                ? mood
                  ? `${name}${moodLine(mood.valence, mood.arousal)}`
                  : `${name}在这儿呢`
                : `${name}睡得正香`}
            </p>
            {mood && <span className="chip">心情 · {EMOTION_CN[mood.emotion] ?? mood.emotion_cn}</span>}
            <p className="home__since">
              {persona.running
                ? `已经醒了 ${duration(metrics?.uptime_seconds ?? 0)}`
                : convo?.last_activity_at
                  ? `上次说话是 ${ago(convo.last_activity_at)}`
                  : '还没有说过话'}
            </p>
          </div>
        </div>
        <div className="home__actions">
          <button
            className={`soft-btn ${persona.running ? 'ghost' : 'blush'}`}
            onClick={toggle}
            disabled={wakeSleep.isPending}
          >
            {persona.running ? <MoonIcon width={18} height={18} /> : <SunIcon width={18} height={18} />}
            {wakeSleep.isPending ? '稍等……' : persona.running ? '哄她睡觉' : '叫醒她'}
          </button>
          <span className="home__pat hand">（也可以摸摸她的头）</span>
        </div>
        {wakeSleep.isError && <p className="home__warn">{(wakeSleep.error as Error).message}</p>}
        {adapterOffline && (
          <Link href="/mailbox" className="home__warn home__warn--link">
            QQ 那边好像没连上，去信箱看看？
          </Link>
        )}
      </motion.article>

      <motion.article className="home__day paper" variants={card}>
        <h2 className="home__h hand">这一天</h2>
        <div className="home__stats">
          <div>
            <CountUp value={convo?.heard ?? 0} className="home__n" />
            <span>听大家说了</span>
          </div>
          <div>
            <CountUp value={convo?.replied ?? 0} className="home__n" />
            <span>她回了几句</span>
          </div>
          <div>
            <CountUp value={glance?.memory.new ?? 0} className="home__n" />
            <span>记住新的事</span>
          </div>
          <div>
            <CountUp value={convo?.groups.length ?? 0} className="home__n" />
            <span>去过的群</span>
          </div>
        </div>
        <p className="home__foot">
          一共记得 <b className="num">{(glance?.memory.total ?? 0).toLocaleString('zh-CN')}</b> 件事 · 说过{' '}
          <b className="num">{(metrics?.token_usage?.call_count ?? 0).toLocaleString('zh-CN')}</b> 次话
        </p>
      </motion.article>

      {!!convo?.recent.length && (
        <motion.article className="home__recent paper" variants={card}>
          <h2 className="home__h hand">刚才大家在聊</h2>
          <ul>
            {convo.recent.slice(0, 5).map((line, i) => (
              <li key={`${line.timestamp}-${i}`} className={line.role === 'assistant' ? 'is-her' : ''}>
                <span className="home__who">
                  {line.role === 'assistant' ? name : line.speaker_name || '有人'}
                  <em>
                    {groupName(line.group_id)} · {ago(line.timestamp)}
                  </em>
                </span>
                <span className="home__said">{line.content}</span>
              </li>
            ))}
          </ul>
          <Link href="/diary" className="home__more hand">
            翻翻日记 →
          </Link>
        </motion.article>
      )}

      {!!glance?.episodes.length && (
        <motion.article className="home__self paper" variants={card}>
          <h2 className="home__h hand">她自己做的事</h2>
          <ul>
            {glance.episodes.slice(0, 3).map((ep, i) => (
              <li key={`${ep.at}-${i}`}>
                <span className="hand">{ep.seed || ep.kind}</span>
                <em>{ago(ep.at)}</em>
              </li>
            ))}
          </ul>
        </motion.article>
      )}
    </motion.section>
  );
}
