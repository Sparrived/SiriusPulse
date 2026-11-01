import { useMutation } from '@tanstack/react-query';
import { AnimatePresence } from 'motion/react';
import { useState } from 'react';
import { queryClient } from '@/lib/queries';
import { saveMcp, useMcp, type McpServer } from '@/lib/queries-house';
import { useLive } from '@/state/live';
import { Drawer } from '@/ui/Drawer';
import { Field, Segmented, TextInput, Toggle } from '@/ui/form/fields';

// 与后端 _SERVER_NAME_RE 一致：不能含斜杠和换行，最长 80 个字符
const NAME_RE = /^[^/\\\u0000\r\n]{1,80}$/;

function KeyValues({
  value,
  onChange,
  mask,
  placeholder,
}: {
  value: Record<string, string>;
  onChange: (v: Record<string, string>) => void;
  mask: string;
  placeholder: string;
}) {
  const rows = Object.entries(value);
  const set = (rows: [string, string][]) => onChange(Object.fromEntries(rows.filter(([k]) => k)));
  return (
    <div className="kv">
      {rows.map(([k, v], i) => (
        <span key={i} className="kv__row">
          <input
            className="input input--mono"
            value={k}
            placeholder="名字"
            onChange={(e) => set(rows.map((r, j) => (j === i ? [e.target.value, r[1]] : r)))}
          />
          <input
            className="input input--mono"
            type={v === mask ? 'password' : 'text'}
            value={v === mask ? '' : v}
            placeholder={v === mask ? '已保存（留空不改）' : '值'}
            onChange={(e) =>
              set(rows.map((r, j) => (j === i ? [r[0], e.target.value || (v === mask ? mask : '')] : r)))
            }
          />
          <button
            type="button"
            className="groups__remove"
            onClick={() => set(rows.filter((_, j) => j !== i))}
          >
            ×
          </button>
        </span>
      ))}
      <button
        type="button"
        className="pf-add"
        onClick={() => onChange({ ...value, [`${placeholder}_${rows.length + 1}`]: '' })}
      >
        ＋ 加一条
      </button>
    </div>
  );
}

function ServerDrawer({
  name,
  server,
  taken,
  mask,
  onSave,
  onRemove,
  onClose,
  busy,
}: {
  name: string;
  server: McpServer;
  taken: string[];
  mask: string;
  onSave: (name: string, server: McpServer) => void;
  onRemove?: () => void;
  onClose: () => void;
  busy: boolean;
}) {
  const [n, setN] = useState(name);
  const [s, setS] = useState<McpServer>(server);
  const [args, setArgs] = useState((server.args ?? []).join('\n'));
  const patch = (p: Partial<McpServer>) => setS({ ...s, ...p });
  const nameProblem = !NAME_RE.test(n.trim())
    ? '名字不能为空，也不能包含斜杠或换行'
    : n !== name && taken.includes(n)
      ? '已经有同名的了'
      : null;
  const stdio = s.transport === 'stdio';

  return (
    <Drawer
      kicker="MCP 外援"
      title={name ? n : '请一位新外援'}
      onClose={onClose}
      footer={
        <>
          {onRemove && (
            <button className="star-card__farewell" onClick={onRemove}>
              不请它了…
            </button>
          )}
          <span className="drawer__spacer" />
          <button className="soft-btn ghost" onClick={onClose}>
            算了
          </button>
          <button
            className="soft-btn"
            disabled={!!nameProblem || busy || (stdio ? !s.command : !s.url)}
            onClick={() =>
              onSave(n, {
                ...s,
                args: args
                  .split('\n')
                  .map((a) => a.trim())
                  .filter(Boolean),
              })
            }
          >
            收好
          </button>
        </>
      }
    >
      <p className="field__hint">MCP 服务器能给她带来一整套新本领；配置保存后她会自动重新连接。</p>
      <Field label="叫它什么" hint={nameProblem ?? undefined}>
        <TextInput value={n} onChange={setN} mono />
      </Field>
      <Toggle on={s.enabled} onChange={(enabled) => patch({ enabled })} label="启用" />
      <Field label="怎么连">
        <Segmented
          size="sm"
          value={s.transport}
          onChange={(transport) => patch({ transport })}
          options={[
            { value: 'stdio', label: '本地程序 stdio' },
            { value: 'streamable_http', label: 'HTTP' },
            { value: 'sse', label: 'SSE' },
          ]}
        />
      </Field>
      {stdio ? (
        <>
          <Field label="启动命令">
            <TextInput value={s.command ?? ''} onChange={(command) => patch({ command })} mono />
          </Field>
          <Field label="参数" hint="每行一个">
            <textarea
              className="input input--mono"
              rows={4}
              value={args}
              onChange={(e) => setArgs(e.target.value)}
            />
          </Field>
          <Field label="工作目录" hint="可留空">
            <TextInput value={s.cwd ?? ''} onChange={(cwd) => patch({ cwd })} mono />
          </Field>
          <Field label="环境变量">
            <KeyValues value={s.env ?? {}} onChange={(env) => patch({ env })} mask={mask} placeholder="ENV" />
          </Field>
        </>
      ) : (
        <>
          <Field label="地址">
            <TextInput value={s.url ?? ''} onChange={(url) => patch({ url })} mono inputMode="url" />
          </Field>
          <Field label="请求头">
            <KeyValues
              value={s.headers ?? {}}
              onChange={(headers) => patch({ headers })}
              mask={mask}
              placeholder="Header"
            />
          </Field>
        </>
      )}
    </Drawer>
  );
}

/** 外援：MCP 服务器。每一位外援带来一套工具 */
export function McpPanel({ persona }: { persona: string }) {
  const { data, isLoading } = useMcp();
  const [edit, setEdit] = useState<{ name: string; server: McpServer } | null>(null);
  const servers = data?.servers ?? {};
  const mask = data?.secret_mask ?? '********';
  const save = useMutation({
    mutationFn: (next: Record<string, McpServer>) => saveMcp(persona, next),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['mcp', persona] });
      useLive.getState().think('feel', '外援名单更新啦');
      setEdit(null);
    },
  });

  if (isLoading) return <p className="mirror__loading hand">翻通讯录……</p>;
  const names = Object.keys(servers);

  return (
    <>
      <p className="bag__lead">外援是通过 MCP 连接的工具服务器，每位外援会给她带来一套新本领。</p>
      {save.isError && <p className="star-card__error">{(save.error as Error).message}</p>}
      <ul className="mcp-list">
        {names.map((name) => {
          const s = servers[name];
          return (
            <li key={name} className={`paper mcp${s.enabled ? '' : ' is-off'}`}>
              <button className="mcp__body" onClick={() => setEdit({ name, server: s })}>
                <span className="hand mcp__name">{name}</span>
                <code>{s.transport === 'stdio' ? [s.command, ...(s.args ?? [])].join(' ') : s.url}</code>
              </button>
              <Toggle
                on={s.enabled}
                onChange={(enabled) => save.mutate({ ...servers, [name]: { ...s, enabled } })}
                label=""
              />
            </li>
          );
        })}
      </ul>
      {!names.length && <p className="field__hint">还没有请外援。</p>}
      <button
        className="soft-btn ghost"
        onClick={() =>
          setEdit({ name: '', server: { enabled: true, transport: 'stdio', command: '', args: [] } })
        }
      >
        ＋ 请一位外援
      </button>
      <AnimatePresence>
        {edit && (
          <ServerDrawer
            name={edit.name}
            server={edit.server}
            taken={names}
            mask={mask}
            busy={save.isPending}
            onClose={() => setEdit(null)}
            onRemove={
              edit.name
                ? () =>
                    save.mutate(Object.fromEntries(Object.entries(servers).filter(([k]) => k !== edit.name)))
                : undefined
            }
            onSave={(name, server) => {
              const next = Object.fromEntries(Object.entries(servers).filter(([k]) => k !== edit.name));
              save.mutate({ ...next, [name]: server });
            }}
          />
        )}
      </AnimatePresence>
    </>
  );
}
