import { motion } from 'motion/react';
import { EMOTION_COLOR, HEAT, INTENT, REASON, STRATEGY, weather } from '@/lib/feelings';
import { EMOTION_CN } from '@/lib/mood';
import { useCognition, useCognitionAnalysis } from '@/lib/queries-journal';
import { ago } from '@/lib/time';
import { Sheet } from '@/ui/form/fields';
import { DayClock, EmotionField, MiniHistogram, SoftBars, WeatherGlyph } from './charts';

const top = (rec: Record<string, number> | undefined, n: number) =>
  Object.entries(rec ?? {})
    .filter(([, v]) => v > 0)
    .sort((a, b) => b[1] - a[1])
    .slice(0, n);

const face = (v: number) => (v > 0.3 ? '(◕ᴗ◕✿)' : v > 0.05 ? '(・ᴗ・)' : v > -0.2 ? '(・_・)' : '(︶︹︶)');

/** 情绪天气：她怎么感受身边的人——大家最近心情如何、什么时候热闹、她又是怎么决定要不要说话的 */
export function Weather({ name }: { name: string }) {
  const { data: cog, isLoading } = useCognition();
  const { data: ana } = useCognitionAnalysis();
  const events = cog?.events ?? [];

  if (isLoading) return <p className="mirror__loading hand">看看天……</p>;
  if (!events.length) {
    return (
      <section className="sheet paper">
        <p className="hand">还没有天气记录。</p>
        <p className="field__hint">
          {name}每读一句群里的话，都会感受一下对方的心情。聊起来以后，这里就会有天气了。
        </p>
      </section>
    );
  }

  const avg = events.reduce((s, e) => s + e.valence, 0) / events.length;
  const spread = Math.sqrt(events.reduce((s, e) => s + (e.valence - avg) ** 2, 0) / events.length);
  const w = weather(avg, spread);
  const emotions = top(cog?.emotion_distribution, 8);
  const mainEmotion = emotions.find(([k]) => k !== 'NEUTRAL')?.[0];
  const strategies = top(ana?.strategy_distribution, 5);
  const reasons = top(ana?.decision_summary?.reason_distribution, 6);
  const heat = top(ana?.decision_summary?.heat_distribution, 3);
  const timeline = [...(ana?.decision_timeline ?? [])].reverse();

  return (
    <>
      <motion.section
        className="sheet paper forecast"
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
      >
        <WeatherGlyph sky={w.sky} />
        <div>
          <p className="forecast__kicker">最近 {events.length} 句话里的天气</p>
          <h2 className="hand forecast__text">{w.text}</h2>
          <p className="forecast__sub">
            {mainEmotion ? `感觉到最多的是「${EMOTION_CN[mainEmotion]}」` : '大多数时候大家都挺平静'}
            {heat.length > 0 && ` · 群里大多${HEAT[heat[0][0]] ?? heat[0][0]}`}
          </p>
        </div>
      </motion.section>

      <Sheet title="大家的心情落在哪里">
        <EmotionField events={events} />
        <SoftBars
          items={emotions.map(([k, v]) => ({ key: k, label: EMOTION_CN[k] ?? k, value: v }))}
          color={(k) => EMOTION_COLOR[k] ?? '#c8cde3'}
        />
      </Sheet>

      <div className="heart__pair">
        <Sheet title="什么时候最热闹">
          <DayClock hours={ana?.hourly_distribution ?? {}} />
        </Sheet>
        <Sheet title="大家找她是想">
          <SoftBars
            items={top(ana?.intent_distribution, 6).map(([k, v]) => ({
              key: k,
              label: INTENT[k] ?? k,
              value: v,
            }))}
            color={() => '#ffb6c8'}
          />
          <div className="heart__hists">
            <span>
              被点名的程度
              <MiniHistogram h={ana?.score_histograms?.directed} color="#6d88e6" />
            </span>
            <span>
              阴阳怪气
              <MiniHistogram h={ana?.score_histograms?.sarcasm} color="#b28ad8" />
            </span>
            <span>
              理所当然
              <MiniHistogram h={ana?.score_histograms?.entitlement} color="#f0a868" />
            </span>
          </div>
        </Sheet>
      </div>

      {(ana?.decision_summary?.total ?? 0) > 0 && (
        <Sheet title="她怎么决定要不要说话">
          <div className="decide__strip">
            {strategies.map(([k, v]) => (
              <span
                key={k}
                className={`decide__seg decide__seg--${k}`}
                style={{ flex: v }}
                title={`${STRATEGY[k] ?? k} ${v} 次`}
              >
                {STRATEGY[k] ?? k} <b className="num">{v}</b>
              </span>
            ))}
          </div>
          <div className="decide__timeline" aria-label="最近的决定">
            {timeline.map((d, i) => (
              <span
                key={i}
                className={`decide__dot decide__dot--${d.strategy}`}
                style={{ bottom: `${Math.min(1, d.score) * 100}%` }}
                title={`${STRATEGY[d.strategy] ?? d.strategy} · ${REASON[d.reason] ?? d.reason} · ${d.score.toFixed(2)}`}
              />
            ))}
            {ana?.decision_summary?.avg_threshold != null && (
              <i className="decide__line" style={{ bottom: `${ana.decision_summary.avg_threshold * 100}%` }}>
                门槛
              </i>
            )}
          </div>
          <p className="field__hint">每个点是一次决定：越高越想说话，超过门槛线她才会开口。</p>
          <ul className="decide__reasons">
            {reasons.map(([k, v]) => (
              <li key={k}>
                <span>{REASON[k] ?? k}</span>
                <b className="num">{v}</b>
              </li>
            ))}
          </ul>
        </Sheet>
      )}

      {!!ana?.group_summary?.length && (
        <Sheet title="各个群的气氛">
          <ul className="groups-mood">
            {ana.group_summary.map((g) => (
              <li key={g.group_id}>
                <span className="groups-mood__face">{face(g.avg_valence)}</span>
                <span className="num">群 {g.group_id}</span>
                <span className="groups-mood__meta">
                  {g.event_count} 句 · {g.unique_users} 人 · 上次{' '}
                  {ago(new Date(g.last_event * 1000).toISOString())}
                </span>
              </li>
            ))}
          </ul>
        </Sheet>
      )}
    </>
  );
}
