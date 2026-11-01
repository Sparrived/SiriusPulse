import { useMutation } from '@tanstack/react-query';
import { AnimatePresence, motion } from 'motion/react';
import { useState } from 'react';
import { queryClient } from '@/lib/queries';
import { saveToolConfig, toggleTool, useModels, useTools, type ToolInfo } from '@/lib/queries-house';
import { useLive } from '@/state/live';
import { Drawer } from '@/ui/Drawer';
import { Toggle } from '@/ui/form/fields';
import { ParamForm, useJsonErrors, withDefaults, type Settings } from '@/ui/params/ParamForm';
import { hueOf, toolName } from './names';

function ToolDrawer({ persona, tool, onClose }: { persona: string; tool: ToolInfo; onClose: () => void }) {
  const { data: models } = useModels();
  const declared = new Set(tool.config_parameters.map((p) => p.name));
  const extraKeys = Object.keys(tool.config ?? {}).filter((k) => !declared.has(k));
  const [config, setConfig] = useState<Settings>(() => withDefaults(tool.config_parameters, tool.config));
  const [extra, setExtra] = useState(() =>
    extraKeys.length
      ? JSON.stringify(Object.fromEntries(extraKeys.map((k) => [k, tool.config[k]])), null, 2)
      : '',
  );
  const [enabled, setEnabled] = useState(tool.enabled);
  const json = useJsonErrors();
  let extraObj: Settings = {};
  let extraError: string | null = null;
  try {
    extraObj = extra.trim() ? JSON.parse(extra) : {};
    if (!extraObj || typeof extraObj !== 'object' || Array.isArray(extraObj))
      throw new Error('需要一个 JSON 对象');
  } catch (e) {
    extraError = (e as Error).message;
  }

  const save = useMutation({
    mutationFn: () => saveToolConfig(persona, tool.name, { ...extraObj, ...config }, enabled),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tools', persona] });
      useLive.getState().think('feel', `「${toolName(tool.name)}」的用法记住了`);
      onClose();
    },
  });

  return (
    <Drawer
      kicker={`${tool.name} · v${tool.version}`}
      title={toolName(tool.name)}
      onClose={onClose}
      footer={
        <>
          {save.isError && <span className="star-card__error">{(save.error as Error).message}</span>}
          <span className="drawer__spacer" />
          <button className="soft-btn ghost" onClick={onClose}>
            算了
          </button>
          <button
            className="soft-btn"
            disabled={save.isPending || json.hasErrors || !!extraError}
            onClick={() => save.mutate()}
          >
            {save.isPending ? '收好中……' : '收好'}
          </button>
        </>
      }
    >
      <p className="bag__desc">{tool.description}</p>
      <div className="bag__tags">
        {tool.developer_only && <span className="chip">只听主人的</span>}
        {tool.silent && <span className="chip">悄悄用，不说出来</span>}
        {tool.tags.map((t) => (
          <span key={t} className="bag__tag">
            #{t}
          </span>
        ))}
      </div>
      <Toggle on={enabled} onChange={setEnabled} label="允许她用这样东西" />

      {tool.config_parameters.length > 0 ? (
        <ParamForm
          params={tool.config_parameters}
          value={config}
          onChange={setConfig}
          models={models?.model_choices}
          onJsonErrors={json.report}
        />
      ) : (
        <p className="field__hint">这样东西不需要额外设置。</p>
      )}

      <details className="inner__fold">
        <summary className="hand">更多设置（JSON）</summary>
        <p className="field__hint">没有在上面列出的配置项，可以直接写在这里。</p>
        <textarea
          className="input input--mono pf-json"
          rows={5}
          value={extra}
          onChange={(e) => setExtra(e.target.value)}
          placeholder="{}"
        />
        {extraError && <span className="chips__error">{extraError}</span>}
      </details>

      {tool.parameters.length > 0 && (
        <details className="inner__fold">
          <summary className="hand">她用的时候会填写（{tool.parameters.length}）</summary>
          <ul className="bag__params">
            {tool.parameters.map((p) => (
              <li key={p.name}>
                <code>{p.name}</code>
                <em>{p.type}</em>
                {p.required && <i>必填</i>}
                <span>{p.description}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </Drawer>
  );
}

/** 本领：她背包里的小工具，开关就是「允不允许她用」 */
export function ToolsShelf({ persona }: { persona: string }) {
  const { data, isLoading, isError, error } = useTools();
  const [open, setOpen] = useState<ToolInfo | null>(null);
  const toggle = useMutation({
    mutationFn: ({ name, on }: { name: string; on: boolean }) => toggleTool(persona, name, on),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['tools', persona] }),
  });
  if (isError) return <p className="mirror__error">{(error as Error).message}</p>;
  if (isLoading) return <p className="mirror__loading hand">翻开背包……</p>;
  const tools = data?.tools ?? [];

  return (
    <>
      <p className="bag__lead">
        她的背包里有 <b className="num">{tools.length}</b> 样东西，
        <b className="num">{tools.filter((t) => t.enabled).length}</b> 样允许使用。点一样看看怎么用。
      </p>
      <ul className="shelf">
        {tools.map((t, i) => (
          <motion.li
            key={t.name}
            className={`item paper${t.enabled ? '' : ' is-off'}`}
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: i * 0.025 }}
          >
            <button className="item__body" onClick={() => setOpen(t)}>
              <span className="item__glyph" style={{ background: hueOf(t.name) }}>
                {toolName(t.name).slice(0, 1)}
              </span>
              <span className="item__name hand">{toolName(t.name)}</span>
              <code className="item__code">{t.name}</code>
              <span className="item__desc">{t.description}</span>
              <span className="item__meta">
                {t.developer_only && '只听主人的 · '}
                {t.config_parameters.length ? `${t.config_parameters.length} 项设置` : '无需设置'}
              </span>
            </button>
            <span className="item__switch">
              <Toggle on={t.enabled} onChange={(on) => toggle.mutate({ name: t.name, on })} label="" />
            </span>
          </motion.li>
        ))}
      </ul>
      <AnimatePresence>
        {open && <ToolDrawer persona={persona} tool={open} onClose={() => setOpen(null)} />}
      </AnimatePresence>
    </>
  );
}
