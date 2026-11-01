import { useMutation } from '@tanstack/react-query';
import { motion } from 'motion/react';
import { useState } from 'react';
import { LIFESPAN, MEMORY_KINDS, SCOPES } from '@/lib/memory-kinds';
import { queryClient } from '@/lib/queries';
import { forgetMemory, saveMemory } from '@/lib/queries-journal';
import type { MemoryUnit } from '@/lib/types-journal';
import { useLive } from '@/state/live';
import { useScene } from '@/state/scene';
import { Chips, Field, Segmented, Slider, TextInput, Toggle } from '@/ui/form/fields';

export function blankMemory(group: string): MemoryUnit {
  return {
    unit_id: '',
    group_id: group,
    created_at: '',
    unit_type: 'note',
    scope: 'group',
    scope_id: '',
    summary: '',
    participants: [],
    topics: [],
    keywords: [],
    salience: 0.6,
    confidence: 0.8,
    lifespan: 'medium',
    should_prompt: true,
    source_ids: [],
  };
}

/**
 * 翻看/修改一件回忆。和别的房间不同，这里是明确的「收好」按钮：
 * 每次保存都会让她重新整理这条记忆的检索，不适合边打字边存。
 */
export function MemoryEditor({
  persona,
  unit,
  groups,
  onDone,
}: {
  persona: string;
  unit: MemoryUnit;
  groups: string[];
  onDone: () => void;
}) {
  const isNew = !unit.unit_id;
  const [draft, setDraft] = useState<MemoryUnit>(unit);
  const [confirmForget, setConfirmForget] = useState(false);
  const patch = (p: Partial<MemoryUnit>) => setDraft((d) => ({ ...d, ...p }));
  const dirty = JSON.stringify(draft) !== JSON.stringify(unit);

  const done = (line: string) => {
    queryClient.invalidateQueries({ queryKey: ['memories', persona] });
    useLive.getState().think('feel', line);
    onDone();
  };
  const save = useMutation({
    mutationFn: () => saveMemory(persona, { ...draft, summary: draft.summary.trim() }, isNew),
    onSuccess: (res) => {
      if (isNew && res?.unit) useScene.getState().set({ focusMemory: res.unit.unit_id });
      done(isNew ? '嗯，这件事我记下了' : '记得更清楚了');
    },
  });
  const forget = useMutation({
    mutationFn: () => forgetMemory(persona, unit.unit_id),
    onSuccess: () => {
      useScene.getState().set({ focusMemory: null });
      done('……那件事，我不记得了');
    },
  });

  return (
    <motion.div
      className="memory-editor"
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      exit={{ opacity: 0, height: 0 }}
    >
      <Field label="这件事是">
        <textarea
          className="letter hand memory-editor__summary"
          value={draft.summary}
          rows={3}
          maxLength={180}
          placeholder="用一句话写下来，比如：临雀喝咖啡不加糖"
          onChange={(e) => patch({ summary: e.target.value })}
        />
      </Field>
      <div className="memory-editor__kinds">
        {MEMORY_KINDS.map((k) => (
          <button
            key={k.value}
            type="button"
            className={`kind-chip${draft.unit_type === k.value ? ' is-on' : ''}`}
            style={{ '--kind': k.color } as React.CSSProperties}
            onClick={() => patch({ unit_type: k.value })}
          >
            {k.label}
          </button>
        ))}
      </div>
      <div className="grid-2">
        <Field label="记在哪个群">
          {isNew ? (
            <TextInput
              value={draft.group_id}
              onChange={(group_id) => patch({ group_id: group_id.trim() })}
              mono
              placeholder={groups[0] ?? '群号'}
            />
          ) : (
            <TextInput value={draft.group_id} onChange={() => {}} readOnly mono />
          )}
        </Field>
        <Field label="关于">
          <Segmented size="sm" value={draft.scope} options={SCOPES} onChange={(scope) => patch({ scope })} />
        </Field>
      </div>
      {draft.scope === 'user' && (
        <Field label="关于谁" hint="对方的名字或 ID">
          <TextInput value={draft.scope_id} onChange={(scope_id) => patch({ scope_id })} />
        </Field>
      )}
      <Field label="有谁在">
        <Chips
          values={draft.participants}
          onChange={(participants) => patch({ participants })}
          placeholder="名字，回车添加"
        />
      </Field>
      <div className="grid-2">
        <Field label="话题">
          <Chips values={draft.topics} onChange={(topics) => patch({ topics })} placeholder="回车添加" />
        </Field>
        <Field label="提到这些词时会想起">
          <Chips
            values={draft.keywords}
            onChange={(keywords) => patch({ keywords })}
            placeholder="回车添加"
          />
        </Field>
      </div>
      <Field label="有多重要">
        <Slider
          value={draft.salience}
          onChange={(salience) => patch({ salience })}
          low="随口一提"
          high="刻骨铭心"
        />
      </Field>
      <Field label="有多确定">
        <Slider
          value={draft.confidence}
          onChange={(confidence) => patch({ confidence })}
          low="好像是"
          high="千真万确"
        />
      </Field>
      <Field label="能记多久">
        <Segmented
          size="sm"
          value={draft.lifespan}
          options={LIFESPAN}
          onChange={(lifespan) => patch({ lifespan })}
        />
      </Field>
      <Toggle
        on={draft.should_prompt}
        onChange={(should_prompt) => patch({ should_prompt })}
        label="聊天时会想起它"
        sub="关掉的话这件事还在瓶子里，但回话时不会被翻出来"
      />
      {!!draft.source_ids?.length && <p className="field__hint">来自 {draft.source_ids.length} 句聊天记录</p>}

      {(save.isError || forget.isError) && (
        <p className="star-card__error">{((save.error ?? forget.error) as Error).message}</p>
      )}
      <div className="memory-editor__actions">
        {!isNew &&
          (confirmForget ? (
            <span className="memory-editor__confirm">
              真的要她忘掉吗？
              <button className="soft-btn ghost" onClick={() => setConfirmForget(false)}>
                不了
              </button>
              <button className="soft-btn blush" disabled={forget.isPending} onClick={() => forget.mutate()}>
                忘掉
              </button>
            </span>
          ) : (
            <button className="star-card__farewell" onClick={() => setConfirmForget(true)}>
              让她忘掉…
            </button>
          ))}
        <span className="memory-editor__spacer" />
        <button className="soft-btn ghost" onClick={onDone}>
          {dirty ? '不改了' : '合上'}
        </button>
        <button
          className="soft-btn"
          disabled={!draft.summary.trim() || !draft.group_id || save.isPending || (!dirty && !isNew)}
          onClick={() => save.mutate()}
        >
          {save.isPending ? '收好中……' : isNew ? '放进瓶子' : '收好'}
        </button>
      </div>
    </motion.div>
  );
}
