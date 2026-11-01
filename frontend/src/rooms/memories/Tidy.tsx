import { useMutation } from '@tanstack/react-query';
import { motion } from 'motion/react';
import { useEffect } from 'react';
import { queryClient, useWakeSleep } from '@/lib/queries';
import { applyDedupe, startDedupe, useDedupe } from '@/lib/queries-journal';
import type { DedupeChange } from '@/lib/types-journal';
import { useLive } from '@/state/live';
import { useScene } from '@/state/scene';
import { Sheet } from '@/ui/form/fields';

const BUSY = ['queued', 'scanning', 'applying'];

function Change({ c }: { c: DedupeChange }) {
  if (c.decision === 'MERGE') {
    return (
      <li className="tidy__change tidy__change--merge">
        <span className="tidy__pair">
          「{c.target_summary}」<em>＋</em>「{c.incoming_summary}」
        </span>
        <span className="tidy__into hand">→ {c.result_summary}</span>
      </li>
    );
  }
  if (c.decision === 'DUPLICATE') {
    return (
      <li className="tidy__change">
        <span className="tidy__pair">
          「{c.incoming_summary}」<em>和</em>「{c.target_summary}」
        </span>
        <span className="tidy__into">是同一件事，留一份就好</span>
      </li>
    );
  }
  return (
    <li className="tidy__change tidy__change--conflict">
      <span className="tidy__pair">
        「{c.incoming_summary}」<em>和</em>「{c.target_summary}」
      </span>
      <span className="tidy__into">说法对不上，两条都先留着</span>
    </li>
  );
}

/**
 * 整理回忆：让她把重复的、能合在一起的记忆理一理。
 * 先扫描出一份计划，看清楚哪些会合并，再决定要不要照做（应用前会自动备份）。
 */
export function Tidy({ persona, awake, onClose }: { persona: string; awake: boolean; onClose: () => void }) {
  const { data: status } = useDedupe(true);
  const wake = useWakeSleep();
  const state = status?.status ?? 'idle';
  const busy = BUSY.includes(state);

  useEffect(() => {
    useScene.getState().set({ jarBusy: busy });
    return () => useScene.getState().set({ jarBusy: false });
  }, [busy]);

  useEffect(() => {
    if (state === 'completed') {
      queryClient.invalidateQueries({ queryKey: ['memories', persona] });
    }
  }, [state, persona]);

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['dedupe', persona] });
  const scan = useMutation({ mutationFn: () => startDedupe(persona), onSettled: refresh });
  const apply = useMutation({
    mutationFn: () => applyDedupe(persona, status!.job_id!),
    onSuccess: () => useLive.getState().think('feel', '我把回忆理一理，等我一下～'),
    onSettled: refresh,
  });
  const report = status?.report;

  return (
    <Sheet
      title="整理回忆"
      aside={
        <button className="sheet__fold" onClick={onClose}>
          收起
        </button>
      }
      className="tidy"
    >
      {!awake && !busy && state !== 'ready' && (
        <div className="tidy__sleep">
          <p>整理回忆要她醒着才行：她会一件件重新想一遍，把重复的合在一起。</p>
          <button
            className="soft-btn blush"
            disabled={wake.isPending}
            onClick={() => wake.mutate({ name: persona, wake: true })}
          >
            叫醒她
          </button>
        </div>
      )}

      {(state === 'idle' || state === 'completed' || state === 'stale' || state === 'failed') && awake && (
        <div className="tidy__start">
          {state === 'completed' && <p className="hand">上次整理完了 ✓（整理前的样子已经备份）</p>}
          {state === 'stale' && <p>上次的整理计划过期了：那之后回忆又有了变化，重新看一遍吧。</p>}
          {state === 'failed' && <p className="star-card__error">上次整理没成功：{status?.error}</p>}
          <p className="field__hint">
            她会先把每件事和相似的回忆比一比，列出一份整理计划给你看；你点头了才会真的改。
          </p>
          <button className="soft-btn" disabled={scan.isPending} onClick={() => scan.mutate()}>
            让她看一遍
          </button>
          {scan.isError && <p className="star-card__error">{(scan.error as Error).message}</p>}
        </div>
      )}

      {busy && (
        <div className="tidy__progress">
          <p className="hand">
            {state === 'applying' ? '正在整理……' : state === 'queued' ? '准备翻开瓶子……' : '一件一件地翻看……'}
          </p>
          <div className="tidy__bar">
            <motion.i animate={{ width: `${status?.progress ?? 5}%` }} />
          </div>
        </div>
      )}

      {state === 'ready' && !report && (
        <div className="tidy__plan">
          <p>整理计划已经备好了，但读不到计划的细节（可能是旧版本留下的）。可以重新看一遍再决定。</p>
          <button className="soft-btn" disabled={!awake || scan.isPending} onClick={() => scan.mutate()}>
            重新看一遍
          </button>
        </div>
      )}

      {state === 'ready' && report && (
        <div className="tidy__plan">
          <p>
            看完了 <b className="num">{report.total}</b> 件事：
            {report.changes_total ? (
              <>
                有 <b className="num">{report.changes_total}</b> 处可以整理。
              </>
            ) : (
              '都挺好的，没有需要整理的。'
            )}
          </p>
          <ul>
            {report.changes.map((c, i) => (
              <Change key={i} c={c} />
            ))}
          </ul>
          {report.changes_total > report.changes.length && (
            <p className="field__hint">……还有 {report.changes_total - report.changes.length} 处没列出来</p>
          )}
          <div className="tidy__actions">
            <button className="soft-btn ghost" onClick={onClose}>
              先不了
            </button>
            {report.changes_total > 0 && (
              <button
                className="soft-btn blush"
                disabled={!awake || apply.isPending}
                onClick={() => apply.mutate()}
              >
                {awake ? '就这样整理' : '要她醒着才能整理'}
              </button>
            )}
          </div>
          {apply.isError && <p className="star-card__error">{(apply.error as Error).message}</p>}
        </div>
      )}
    </Sheet>
  );
}
