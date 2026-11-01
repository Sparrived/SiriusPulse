import { useMutation } from '@tanstack/react-query';
import { AnimatePresence, motion } from 'motion/react';
import { useEffect, useMemo, useState } from 'react';
import { queryClient } from '@/lib/queries';
import {
  savePluginPermissions,
  savePluginSettings,
  togglePlugin,
  useModels,
  usePluginDetail,
  usePlugins,
  type PluginDetail,
  type PluginInfo,
} from '@/lib/queries-house';
import { useLive } from '@/state/live';
import { Drawer } from '@/ui/Drawer';
import { Chips, Field, Segmented, Toggle } from '@/ui/form/fields';
import { ParamForm, useJsonErrors, withDefaults, type Settings } from '@/ui/params/ParamForm';
import { normalizePluginUISchema } from '@/ui/params/schema';
import { hueOf } from './names';

type Tab = 'settings' | 'rules' | 'about';

function SettingsTab({ plugin, onSaved }: { plugin: PluginDetail; onSaved: () => void }) {
  const { data: models } = useModels();
  // 浏览器边界再校验一次 ui_schema：不合规就整体退回按分组排版
  const schema = useMemo(() => normalizePluginUISchema(plugin.ui_schema, plugin.parameters), [plugin]);
  const [value, setValue] = useState<Settings>(() => withDefaults(plugin.parameters, plugin.settings));
  const json = useJsonErrors();
  const save = useMutation({
    mutationFn: () => savePluginSettings(plugin.name, value),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['plugin', plugin.name] });
      queryClient.invalidateQueries({ queryKey: ['plugins'] });
      useLive.getState().think('feel', `「${plugin.display_name}」设置好了`);
      onSaved();
    },
  });

  if (!plugin.parameters.length) return <p className="field__hint">这个插件没有可以设置的东西。</p>;
  return (
    <>
      <ParamForm
        params={plugin.parameters}
        schema={schema}
        value={value}
        onChange={setValue}
        models={models?.model_choices}
        secretPolicy="declared"
        onJsonErrors={json.report}
      />
      <div className="bag__actions">
        {save.isError && <span className="star-card__error">{(save.error as Error).message}</span>}
        <button
          className="soft-btn"
          disabled={save.isPending || json.hasErrors}
          onClick={() => save.mutate()}
        >
          {save.isPending ? '收好中……' : '收好设置'}
        </button>
      </div>
    </>
  );
}

function RulesTab({ plugin }: { plugin: PluginDetail }) {
  const p = plugin.permissions;
  const [draft, setDraft] = useState({
    developer_only: p.developer_only,
    hidden_from_intent: p.hidden_from_intent,
    rate: p.rate_limit_calls_per_minute,
    group_blacklist: p.group_blacklist,
  });
  const save = useMutation({
    mutationFn: () =>
      savePluginPermissions(plugin.name, {
        ...(p.developer_only_locked ? {} : { developer_only: draft.developer_only }),
        ...(p.hidden_from_intent_locked ? {} : { hidden_from_intent: draft.hidden_from_intent }),
        ...(draft.rate ? { rate_limit_calls_per_minute: draft.rate } : {}),
        group_blacklist: draft.group_blacklist,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['plugin', plugin.name] });
      useLive.getState().think('feel', '规矩记住啦');
    },
  });
  return (
    <>
      <Toggle
        on={draft.developer_only}
        onChange={(developer_only) => !p.developer_only_locked && setDraft({ ...draft, developer_only })}
        label="只听主人的"
        sub={p.developer_only_locked ? '插件自己要求的，不能关掉' : '开着时只有你（root QQ）能用'}
      />
      <Toggle
        on={draft.hidden_from_intent}
        onChange={(hidden_from_intent) =>
          !p.hidden_from_intent_locked && setDraft({ ...draft, hidden_from_intent })
        }
        label="只认命令，不从聊天里猜"
        sub={
          p.hidden_from_intent_locked ? '插件自己要求的，不能关掉' : '关着时她会从自然的聊天里判断要不要用它'
        }
      />
      <Field label="每分钟最多用几次" hint="留空则不修改；填 1–1000">
        <input
          className="input num bag__rate"
          inputMode="numeric"
          value={draft.rate ?? ''}
          onChange={(e) => {
            const n = Math.trunc(Number(e.target.value));
            setDraft({ ...draft, rate: e.target.value && n >= 1 ? Math.min(1000, n) : null });
          }}
        />
      </Field>
      <Field label="这些群里不许用">
        <Chips
          values={draft.group_blacklist}
          onChange={(group_blacklist) => setDraft({ ...draft, group_blacklist })}
          placeholder="群号，回车添加"
        />
      </Field>
      <div className="bag__actions">
        {save.isError && <span className="star-card__error">{(save.error as Error).message}</span>}
        {save.isSuccess && <span className="whisper whisper--saved hand">记下啦</span>}
        <button className="soft-btn" disabled={save.isPending} onClick={() => save.mutate()}>
          收好规矩
        </button>
      </div>
    </>
  );
}

function AboutTab({ plugin }: { plugin: PluginDetail }) {
  return (
    <>
      <p className="bag__desc">{plugin.description}</p>
      <p className="field__hint">
        作者 {plugin.author || '佚名'} · v{plugin.version}
      </p>
      {plugin.commands.map((c) => (
        <div key={c.name} className="cmd">
          <div className="cmd__patterns">
            {c.patterns.map((pt) => (
              <code key={pt}>{pt}</code>
            ))}
            <em>{c.pattern_type === 'prefix' ? '开头' : c.pattern_type}</em>
          </div>
          <p>{c.description}</p>
          {!!c.examples?.length && <p className="field__hint">例如：{c.examples.join('　')}</p>}
        </div>
      ))}
      {plugin.events.map((e, i) => (
        <p key={i} className="field__hint">
          ⏰ {e.type}
          {e.cron && <code> {e.cron}</code>} {e.description}
        </p>
      ))}
      {!!plugin.nl_examples?.length && (
        <div>
          <p className="hand">也可以这样跟她说：</p>
          <ul className="bag__examples">
            {plugin.nl_examples.map((x) => (
              <li key={x}>「{x}」</li>
            ))}
          </ul>
        </div>
      )}
      {plugin.source_content && (
        <details className="inner__fold">
          <summary className="hand">看看源码（{plugin.source_file}）</summary>
          <pre className="inner__prompt bag__source">{plugin.source_content}</pre>
        </details>
      )}
    </>
  );
}

function PluginDrawer({ name, onClose }: { name: string; onClose: () => void }) {
  const { data, isLoading, isError, error } = usePluginDetail(name);
  const [tab, setTab] = useState<Tab>('settings');
  return (
    <Drawer
      kicker={data ? `${data.name} · v${data.version}` : name}
      title={data?.display_name ?? name}
      onClose={onClose}
      wide
    >
      {isLoading && <p className="mirror__loading hand">拆开看看……</p>}
      {isError && <p className="mirror__error">{(error as Error).message}</p>}
      {data && (
        <>
          <Segmented
            value={tab}
            onChange={setTab}
            options={[
              { value: 'settings', label: '设置' },
              { value: 'rules', label: '规矩' },
              { value: 'about', label: '说明' },
            ]}
          />
          {tab === 'settings' && <SettingsTab key={data.name} plugin={data} onSaved={onClose} />}
          {tab === 'rules' && <RulesTab key={data.name} plugin={data} />}
          {tab === 'about' && <AboutTab plugin={data} />}
        </>
      )}
    </Drawer>
  );
}

/** 插件：主人教给她的指令（/开头的那些），全部伙伴共用 */
export function PluginsShelf() {
  const { data, isLoading, isError, error } = usePlugins();
  const [open, setOpen] = useState<string | null>(null);
  const toggle = useMutation({
    mutationFn: ({ name, on }: { name: string; on: boolean }) => togglePlugin(name, on),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['plugins'] }),
  });
  useEffect(() => {
    if (!open) queryClient.invalidateQueries({ queryKey: ['plugins'] });
  }, [open]);
  if (isError) return <p className="mirror__error">{(error as Error).message}</p>;
  if (isLoading) return <p className="mirror__loading hand">翻开插件盒……</p>;
  const plugins: PluginInfo[] = data?.plugins ?? [];

  return (
    <>
      <p className="bag__lead">插件是大家用指令召唤的本领（比如 /github），所有伙伴共用一套。</p>
      {!plugins.length && <p className="field__hint">plugins/ 目录里还没有插件。</p>}
      <ul className="shelf">
        {plugins.map((p, i) => (
          <motion.li
            key={p.name}
            className={`item paper${p.enabled ? '' : ' is-off'}`}
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: i * 0.04 }}
          >
            <button className="item__body" onClick={() => setOpen(p.name)}>
              <span className="item__glyph" style={{ background: hueOf(p.name) }}>
                {p.display_name.slice(0, 1)}
              </span>
              <span className="item__name hand">{p.display_name}</span>
              <code className="item__code">
                {p.commands
                  .flatMap((c) => c.patterns)
                  .slice(0, 2)
                  .join(' ') || p.name}
              </code>
              <span className="item__desc">{p.description}</span>
              <span className="item__meta">
                v{p.version} · {p.parameters.length ? `${p.parameters.length} 项设置` : '无需设置'}
              </span>
            </button>
            <span className="item__switch">
              <Toggle on={p.enabled} onChange={(on) => toggle.mutate({ name: p.name, on })} label="" />
            </span>
          </motion.li>
        ))}
      </ul>
      <AnimatePresence>{open && <PluginDrawer name={open} onClose={() => setOpen(null)} />}</AnimatePresence>
    </>
  );
}
