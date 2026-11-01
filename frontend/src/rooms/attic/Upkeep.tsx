import { useMutation } from '@tanstack/react-query';
import { useState } from 'react';
import { setToken } from '@/lib/auth';
import { queryClient } from '@/lib/queries';
import { rebuildEmbedding, shutdownAll, useEmbedding } from '@/lib/queries-house';
import { Sheet } from '@/ui/form/fields';
import { Stamp } from '@/rooms/mailbox/AdapterCard';

/** 维护：重建回忆索引、关掉整座房子、离开 */
export function Upkeep() {
  const { data: emb } = useEmbedding();
  const [confirmRebuild, setConfirmRebuild] = useState(false);
  const [bye, setBye] = useState('');
  const rebuild = useMutation({
    mutationFn: rebuildEmbedding,
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['embedding'] }),
  });
  const shutdown = useMutation({ mutationFn: shutdownAll });

  return (
    <>
      <Sheet title="回忆的索引" aside={emb && <Stamp state={emb.ready ? 'ok' : 'down'} />}>
        <p className="ws__facts">
          {emb ? (emb.ready ? `用 ${emb.model} 给回忆编目录（经由 AMKR）` : emb.error) : '看看索引……'}
        </p>
        <p className="field__hint">
          换了向量模型以后要重建一次：旧的向量和新模型对不上，她会想不起相关的事。重建会逐条重算，回忆多的时候要等一会儿。
        </p>
        {confirmRebuild ? (
          <div className="ws__actions">
            <span>确定要重建全部回忆的索引吗？</span>
            <button className="soft-btn ghost" onClick={() => setConfirmRebuild(false)}>
              不了
            </button>
            <button
              className="soft-btn"
              disabled={rebuild.isPending}
              onClick={() => {
                setConfirmRebuild(false);
                rebuild.mutate();
              }}
            >
              重建
            </button>
          </div>
        ) : (
          <button
            className="soft-btn ghost"
            disabled={rebuild.isPending || !emb?.ready}
            onClick={() => setConfirmRebuild(true)}
          >
            {rebuild.isPending ? '正在重建……' : '重建索引'}
          </button>
        )}
        {rebuild.data && (
          <p className={rebuild.data.success ? 'whisper whisper--saved hand' : 'star-card__error'}>
            {rebuild.data.success ? `重建好了：${rebuild.data.units} 条回忆` : rebuild.data.error}
          </p>
        )}
        {rebuild.isError && <p className="star-card__error">{(rebuild.error as Error).message}</p>}
      </Sheet>

      <Sheet title="旧版控制台">
        <p className="field__hint">搬家期间旧版还留着，功能齐全；所有房间都搬好以后就会拆掉。</p>
        <a className="soft-btn ghost" href="/legacy/" target="_blank" rel="noreferrer">
          去旧版看看 ↗
        </a>
      </Sheet>

      <Sheet title="出门">
        <p className="field__hint">退出登录，下次回来要重新敲门。</p>
        <button className="soft-btn ghost" onClick={() => setToken(null)}>
          出门
        </button>
      </Sheet>

      <Sheet title="熄灯" fold className="upkeep__danger">
        <p>
          这会关掉整个程序：小屋（WebUI）和所有伙伴都会停下来，要在电脑上重新启动才能回来。输入 <b>晚安</b>{' '}
          确认：
        </p>
        <div className="ws__actions">
          <input
            className="input upkeep__bye"
            value={bye}
            onChange={(e) => setBye(e.target.value)}
            placeholder="晚安"
          />
          <button
            className="soft-btn blush"
            disabled={bye !== '晚安' || shutdown.isPending}
            onClick={() => shutdown.mutate()}
          >
            熄灯
          </button>
        </div>
        {shutdown.isSuccess && <p className="hand">晚安。灯已经熄了。</p>}
      </Sheet>
    </>
  );
}
