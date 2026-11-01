import { queryClient } from '@/lib/queries';
import { saveGlobalConfig, useGlobalConfig, type GlobalConfig } from '@/lib/queries-house';
import { Field, SaveWhisper, Segmented, Sheet, TextInput, Toggle } from '@/ui/form/fields';
import { useAutosave, useDraft } from '@/ui/form/useDraft';

const EDITABLE = [
  'webui_host',
  'webui_port',
  'log_level',
  'amkr_base_url',
  'amkr_local_api_key',
  'amkr_workspace',
  'amkr_public_url',
  'amkr_ui_enabled',
] as const;

function pick(c: GlobalConfig): GlobalConfig {
  return {
    webui_host: c.webui_host ?? '0.0.0.0',
    webui_port: c.webui_port ?? 8080,
    log_level: c.log_level ?? 'INFO',
    amkr_base_url: c.amkr_base_url ?? '',
    amkr_local_api_key: c.amkr_local_api_key ?? '',
    amkr_workspace: c.amkr_workspace ?? '',
    amkr_public_url: c.amkr_public_url ?? '',
    amkr_ui_enabled: c.amkr_ui_enabled ?? true,
  };
}

/** 全局设置：整座房子的门牌（WebUI 地址）与 AMKR 的连接方式。所有伙伴共用。 */
export function Settings() {
  const { data, isFetchedAfterMount, isError, error } = useGlobalConfig();
  const server = data ? pick(data) : undefined;
  const { draft, patch, baseline, setBaseline } = useDraft<GlobalConfig>(
    server,
    'global',
    isFetchedAfterMount,
  );
  const autosave = useAutosave(draft, baseline, setBaseline, async (value) => {
    const port = Number(value.webui_port);
    if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('端口要在 1–65535 之间');
    const body = Object.fromEntries(EDITABLE.map((k) => [k, k === 'webui_port' ? port : value[k]]));
    await saveGlobalConfig(body);
    queryClient.invalidateQueries({ queryKey: ['amkr'] });
    queryClient.invalidateQueries({ queryKey: ['embedding'] });
  });

  if (isError) return <p className="mirror__error">{(error as Error).message}</p>;
  if (!draft) return <p className="mirror__loading hand">翻出房子的图纸……</p>;
  // 服务端把已保存的管理钥匙回显成以 **** 结尾的掩码；原样提交表示不改
  const savedMask = String(server?.amkr_local_api_key ?? '').endsWith('****')
    ? String(server!.amkr_local_api_key)
    : '';
  const keySaved = !!savedMask && draft.amkr_local_api_key === savedMask;

  return (
    <>
      <Sheet title="房子的门牌" aside={<SaveWhisper {...autosave} />}>
        <p className="field__hint">WebUI 在哪个地址开门。改了以后要重启 WebUI 才生效。</p>
        <div className="grid-2">
          <Field label="监听地址" hint="0.0.0.0 表示局域网里都能访问">
            <TextInput
              value={String(draft.webui_host ?? '')}
              onChange={(webui_host) => patch({ webui_host })}
              mono
            />
          </Field>
          <Field label="端口">
            <TextInput
              value={String(draft.webui_port ?? '')}
              onChange={(v) => patch({ webui_port: v.replace(/\D/g, '') as unknown as number })}
              mono
              inputMode="numeric"
            />
          </Field>
        </div>
        <Field label="日志的详细程度">
          <Segmented
            size="sm"
            value={String(draft.log_level ?? 'INFO')}
            onChange={(log_level) => patch({ log_level })}
            options={['DEBUG', 'INFO', 'WARNING', 'ERROR'].map((v) => ({ value: v, label: v }))}
          />
        </Field>
      </Sheet>

      <Sheet title="AMKR：她吃饭的厨房">
        <p className="field__hint">
          她所有的思考都经过本地的 AMKR（模型路由）。这里填 AMKR 在哪、用什么钥匙管理它；模型和参数在 AMKR
          自己的面板里配。
        </p>
        <div className="grid-2">
          <Field label="AMKR 地址">
            <TextInput
              value={String(draft.amkr_base_url ?? '')}
              onChange={(amkr_base_url) => patch({ amkr_base_url })}
              mono
              inputMode="url"
              placeholder="http://127.0.0.1:8000"
            />
          </Field>
          <Field
            label="管理钥匙"
            hint={keySaved ? '已保存；要换就直接输入新的' : '能增删供应商和 Key 的管理员凭据，不会用来聊天'}
          >
            <TextInput
              value={keySaved ? '' : String(draft.amkr_local_api_key ?? '')}
              onChange={(v) => patch({ amkr_local_api_key: v || savedMask })}
              secret
              mono
              placeholder={keySaved ? '已保存（留空不改）' : ''}
            />
          </Field>
          <Field label="工作空间前缀" hint="每位伙伴会在 AMKR 里有一个「前缀/名字」的空间">
            <TextInput
              value={String(draft.amkr_workspace ?? '')}
              onChange={(amkr_workspace) => patch({ amkr_workspace })}
              mono
              placeholder="sirius-pulse"
            />
          </Field>
          <Field label="浏览器访问 AMKR 的地址" hint="留空则通过 WebUI 同源转发">
            <TextInput
              value={String(draft.amkr_public_url ?? '')}
              onChange={(amkr_public_url) => patch({ amkr_public_url })}
              mono
            />
          </Field>
        </div>
        <Toggle
          on={draft.amkr_ui_enabled !== false}
          onChange={(amkr_ui_enabled) => patch({ amkr_ui_enabled })}
          label="在这里内嵌 AMKR 面板"
        />
      </Sheet>
    </>
  );
}
