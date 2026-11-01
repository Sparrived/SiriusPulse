import { useMutation } from '@tanstack/react-query';
import { AnimatePresence } from 'motion/react';
import { useState } from 'react';
import { queryClient, usePersonas } from '@/lib/queries';
import {
  amkrPanel,
  registerAmkr,
  rotateInferenceKey,
  useAmkr,
  type AmkrWorkspace,
} from '@/lib/queries-house';
import { Drawer } from '@/ui/Drawer';
import { Sheet } from '@/ui/form/fields';
import { Stamp } from '@/rooms/mailbox/AdapterCard';

function Workspace({ ws, name, onPanel }: { ws: AmkrWorkspace; name: string; onPanel: () => void }) {
  const [key, setKey] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['amkr'] });
  const register = useMutation({ mutationFn: () => registerAmkr(ws.persona), onSettled: refresh });
  const rotate = useMutation({
    mutationFn: () => rotateInferenceKey(ws.persona),
    onSuccess: (r) => setKey(r.inference_key),
    onSettled: refresh,
  });
  const ok = !ws.error && !ws.missing.length && !ws.unbound.length && ws.inference_ready;

  return (
    <li className={`ws${ok ? ' is-ok' : ''}`}>
      <div className="ws__head">
        <span className="hand ws__name">{name}</span>
        <code>{ws.workspace}</code>
        <span className={`ws__state${ok ? ' is-ok' : ''}`}>
          {ok ? '一切就绪' : ws.error ? '连不上' : '还差一点'}
        </span>
      </div>
      {ws.error && <p className="star-card__error">{ws.error}</p>}
      <p className="ws__facts">
        登记了 <b className="num">{ws.registered.length}</b> 个任务
        {ws.missing.length > 0 && (
          <>
            {' '}
            · 缺 <b className="num">{ws.missing.length}</b> 个：{ws.missing.join('、')}
          </>
        )}
        {ws.unbound.length > 0 && (
          <span className="ws__warn">
            {' '}
            · {ws.unbound.length} 个还没绑模型（{ws.unbound.join('、')}），用到时会 404
          </span>
        )}
      </p>
      <p className="ws__facts">
        推理钥匙 {ws.inference_ready ? '✓' : '✗ 缺少'} · 面板钥匙 {ws.panel_ready ? '✓' : '✗ 缺少'}
      </p>
      <div className="ws__actions">
        {(ws.missing.length > 0 || !ws.registered.length) && (
          <button className="soft-btn" disabled={register.isPending} onClick={() => register.mutate()}>
            {register.isPending ? '登记中……' : '补登记缺的任务'}
          </button>
        )}
        <button className="soft-btn ghost" disabled={rotate.isPending} onClick={() => rotate.mutate()}>
          {ws.inference_ready ? '换一把推理钥匙' : '补一把推理钥匙'}
        </button>
        {ws.panel_ready && (
          <button className="soft-btn ghost" onClick={onPanel}>
            打开她的 AMKR 面板
          </button>
        )}
      </div>
      {(register.isError || rotate.isError) && (
        <p className="star-card__error">{((register.error ?? rotate.error) as Error).message}</p>
      )}
      {key && (
        <div className="ws__key">
          <p>
            新的推理钥匙（<b>只显示这一次</b>，已经自动存好，她会用上它）：
          </p>
          <code>{key}</code>
          <button
            className="soft-btn ghost"
            onClick={() => {
              void navigator.clipboard?.writeText(key);
              setCopied(true);
            }}
          >
            {copied ? '复制好了' : '复制'}
          </button>
        </div>
      )}
    </li>
  );
}

/** AMKR：她吃饭的厨房——连不连得上、每位伙伴的任务登记好了没有 */
export function Amkr() {
  const { data, isLoading } = useAmkr();
  const { data: personas } = usePersonas();
  const [panel, setPanel] = useState<{ persona: string; url?: string; error?: string } | null>(null);
  const registerAll = useMutation({
    mutationFn: () => registerAmkr(),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['amkr'] }),
  });
  if (isLoading || !data) return <p className="mirror__loading hand">去厨房看看……</p>;
  const nameOf = (p: string) => personas?.personas.find((x) => x.name === p)?.persona_name || p;

  const openPanel = async (persona: string) => {
    setPanel({ persona });
    try {
      const r = await amkrPanel(persona);
      setPanel({ persona, url: r.url });
    } catch (e) {
      setPanel({ persona, error: (e as Error).message });
    }
  };

  return (
    <>
      <Sheet title="厨房" aside={<Stamp state={data.reachable ? 'ok' : 'down'} />}>
        <p className="ws__facts">
          {data.reachable
            ? `AMKR ${data.version} 在 ${data.base_url} 开着火`
            : data.error || `连不上 ${data.base_url}`}
        </p>
        {!data.configured && <p className="field__hint">先在「设置」里填好 AMKR 的地址和管理钥匙。</p>}
        <p className="field__hint">
          所有伙伴的工作空间都在 <code>{data.workspace_base}/</code> 下面；她们需要的任务名一共{' '}
          {data.known_tasks.length} 个。
          {data.ops_enabled ? '' : ' AMKR 没有开放运维接口，部分信息看不到。'}
        </p>
        <div className="ws__actions">
          <button
            className="soft-btn"
            disabled={!data.reachable || registerAll.isPending}
            onClick={() => registerAll.mutate()}
          >
            {registerAll.isPending ? '登记中……' : '给所有伙伴补登记'}
          </button>
          {data.ui_url && (
            <a className="soft-btn ghost" href={data.ui_url} target="_blank" rel="noreferrer">
              打开 AMKR 总面板 ↗
            </a>
          )}
        </div>
        {registerAll.isError && <p className="star-card__error">{(registerAll.error as Error).message}</p>}
      </Sheet>

      {data.workspaces.length > 0 && (
        <Sheet title="各位伙伴的灶台">
          <ul className="ws-list">
            {data.workspaces.map((ws) => (
              <Workspace
                key={ws.persona}
                ws={ws}
                name={nameOf(ws.persona)}
                onPanel={() => openPanel(ws.persona)}
              />
            ))}
          </ul>
        </Sheet>
      )}

      <AnimatePresence>
        {panel && (
          <Drawer
            kicker="AMKR 面板"
            title={`${nameOf(panel.persona)}的灶台`}
            onClose={() => setPanel(null)}
            wide
          >
            {panel.error && <p className="mirror__error">{panel.error}</p>}
            {!panel.url && !panel.error && <p className="mirror__loading hand">点火中……</p>}
            {panel.url && <iframe className="amkr-frame" src={panel.url} title="AMKR 面板" />}
          </Drawer>
        )}
      </AnimatePresence>
    </>
  );
}
