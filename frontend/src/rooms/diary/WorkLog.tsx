import { useMutation } from '@tanstack/react-query';
import { motion } from 'motion/react';
import { useState } from 'react';
import { saveWorkTask, useWorkMode } from '@/lib/queries-journal';
import { queryClient } from '@/lib/queries';
import { ago } from '@/lib/time';
import type { WorkSession } from '@/lib/types-journal';
import { Sheet } from '@/ui/form/fields';

const STATUS: Record<string, string> = { running: '正在做', completed: '做完了', aborted: '中途停下了' };

function pretty(raw: string) {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

function minutes(s: WorkSession) {
  if (!s.ended_at) return '';
  const m = Math.round((Date.parse(s.ended_at) - Date.parse(s.started_at)) / 60000);
  return m < 1 ? '不到一分钟' : `${m} 分钟`;
}

function Session({ s }: { s: WorkSession }) {
  const [open, setOpen] = useState(false);
  return (
    <div className={`work work--${s.status}`}>
      <button className="work__head" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        <span className="work__status">{STATUS[s.status] ?? s.status}</span>
        <span className="work__goal hand">{s.goal}</span>
        <span className="work__when">
          {ago(s.started_at)} · {s.steps.length} 步{minutes(s) && ` · ${minutes(s)}`}
        </span>
      </button>
      {s.result && <p className="work__result">结果：{s.result}</p>}
      {open && (
        <motion.ol
          className="work__steps"
          initial={{ opacity: 0, height: 0 }}
          animate={{ opacity: 1, height: 'auto' }}
        >
          {s.steps.map((step, i) => (
            <li key={i}>
              <b className="num">{step.round ?? i + 1}</b>
              <div>
                {step.text && <p>{step.text}</p>}
                {step.tools?.map((t, j) => (
                  <details key={j} className="work__tool">
                    <summary>用了「{t.name}」</summary>
                    <pre>{pretty(t.arguments)}</pre>
                    {step.results?.[j] && <pre className="work__output">{step.results[j].output}</pre>}
                  </details>
                ))}
              </div>
            </li>
          ))}
        </motion.ol>
      )}
    </div>
  );
}

/** 认真工作：她进入工作模式时的每一步，以及工作时用哪个模型 */
export function WorkLog({ persona }: { persona: string }) {
  const { data, isLoading } = useWorkMode();
  const setTask = useMutation({
    mutationFn: (task: string) => saveWorkTask(persona, task),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['work-mode', persona] }),
  });
  if (isLoading || !data) return <p className="mirror__loading hand">翻到工作记录……</p>;

  return (
    <>
      <Sheet title="工作时用哪个模型">
        <p className="field__hint">
          遇到需要好几步才能做完的事，她会自己进入工作模式。这里选那时用的 AMKR 任务；留空就和平时聊天一样。
        </p>
        <select
          className="input worklog__task"
          value={data.settings.task_name}
          disabled={setTask.isPending}
          onChange={(e) => setTask.mutate(e.target.value)}
        >
          <option value="">和平时聊天一样</option>
          {data.task_options.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        {setTask.isError && <p className="star-card__error">{(setTask.error as Error).message}</p>}
      </Sheet>

      {data.sessions.length ? (
        <section className="sheet paper">
          <header className="sheet__head">
            <h2 className="hand">做过的事</h2>
            <span className="field__hint">
              一共 {data.summary.sessions_total} 次，做完 {data.summary.sessions_completed} 次
            </span>
          </header>
          <div className="work__list">
            {data.sessions.map((s, i) => (
              <Session key={s.session_id ?? i} s={s} />
            ))}
          </div>
        </section>
      ) : (
        <section className="sheet paper">
          <p className="hand">她还没有认真工作过。</p>
        </section>
      )}
    </>
  );
}
