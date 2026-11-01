import { Field, SaveWhisper, Sheet, Stepper } from '@/ui/form/fields';
import { DayCurve } from './DayCurve';
import { useExperienceDraft } from './PersonalityTab';

const PRESETS = {
  夜猫子: [
    { time: '03:00', coefficient: 0.6 },
    { time: '09:00', coefficient: 0.3 },
    { time: '14:00', coefficient: 0.9 },
    { time: '22:00', coefficient: 1.6 },
  ],
  早睡早起: [
    { time: '00:00', coefficient: 0.1 },
    { time: '07:00', coefficient: 1.3 },
    { time: '20:00', coefficient: 1.1 },
    { time: '23:00', coefficient: 0.3 },
  ],
  上班族: [
    { time: '08:00', coefficient: 0.9 },
    { time: '10:00', coefficient: 0.5 },
    { time: '12:30', coefficient: 1.4 },
    { time: '15:00', coefficient: 0.5 },
    { time: '19:00', coefficient: 1.5 },
    { time: '01:00', coefficient: 0.2 },
  ],
};

export function RhythmTab({ persona }: { persona: string }) {
  const { draft, patch, autosave, isError, error } = useExperienceDraft(persona);
  if (isError) return <p className="mirror__error">{(error as Error).message}</p>;
  if (!draft) return <p className="mirror__loading hand">正在看她的作息……</p>;

  return (
    <>
      <Sheet title="一天的精神" aside={<SaveWhisper {...autosave} />}>
        <DayCurve
          points={draft.reply_time_curve_points}
          onChange={(reply_time_curve_points) => patch({ reply_time_curve_points })}
        />
        <div className="rhythm__presets">
          <span className="field__hint">照着这些作息来：</span>
          {Object.entries(PRESETS).map(([label, points]) => (
            <button
              key={label}
              type="button"
              className="soft-btn ghost"
              onClick={() => patch({ reply_time_curve_points: points })}
            >
              {label}
            </button>
          ))}
          <button
            type="button"
            className="soft-btn ghost"
            onClick={() => patch({ reply_time_curve_points: [] })}
          >
            全天都一样
          </button>
        </div>
      </Sheet>

      <Sheet title="说话的间隔">
        <div className="grid-2">
          <Field label="两次回话至少隔" hint="0 表示不限制">
            <Stepper
              value={draft.min_reply_interval_seconds}
              onChange={(min_reply_interval_seconds) => patch({ min_reply_interval_seconds })}
              min={0}
              max={3600}
              step={5}
              unit="秒"
            />
          </Field>
          <Field label="认真回一次后歇一会儿" hint="主模型回复之后的冷却">
            <Stepper
              value={draft.main_model_reply_cooldown_seconds}
              onChange={(main_model_reply_cooldown_seconds) => patch({ main_model_reply_cooldown_seconds })}
              min={0}
              max={3600}
              step={5}
              unit="秒"
            />
          </Field>
        </div>
      </Sheet>
    </>
  );
}
