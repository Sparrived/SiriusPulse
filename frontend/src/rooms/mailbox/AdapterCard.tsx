import { AnimatePresence, motion } from 'motion/react';
import { useState } from 'react';
import type { NapCatAdapter } from '@/lib/types';
import { Chips, Field, Segmented, Sheet, Stepper, TextInput, Toggle } from '@/ui/form/fields';

const QQ_RE = /^\d{5,12}$/;
const qqCheck = (v: string) => (QQ_RE.test(v) ? null : `「${v}」不像是 QQ 号或群号`);

export type Strategy = 'smart' | 'keyword';

/** 与后端 NapCatAdapterConfig 的默认值一致：新接一个 QQ 时用 */
export const NEW_ADAPTER: NapCatAdapter = {
  type: 'napcat',
  enabled: true,
  ws_url: 'ws://localhost:3001',
  token: 'napcat_ws',
  qq_number: '',
  allowed_group_ids: [],
  allowed_private_user_ids: [],
  peer_ai_ids: [],
  group_dispatch_enabled: true,
  dispatch_db_path: '',
  dispatch_priority: 0,
  dispatch_min_reply_interval_seconds: 3,
  dispatch_lease_seconds: 120,
  dispatch_peer_cooldown_seconds: 5,
  dispatch_max_peer_turns: 3,
  dispatch_score_collection_seconds: 0.15,
  dispatch_activity_window_seconds: 300,
  dispatch_activity_penalty_per_reply: 0.12,
  dispatch_max_activity_penalty: 0.6,
  enable_group_chat: true,
  enable_private_chat: true,
  root: '',
};

export function Stamp({ state }: { state: 'ok' | 'weak' | 'down' | 'off' | 'sleep' }) {
  const text = { ok: '信号通畅', weak: '信号不太稳', down: '断线了', off: '没寄出', sleep: '她睡着时不连线' }[
    state
  ];
  return <span className={`stamp stamp--${state} hand`}>{text}</span>;
}

/** 一张信封：一个 QQ 号的连接、去哪些群、谁能私聊她 */
export function AdapterCard({
  adapter,
  onChange,
  onRemove,
  strategies,
  onStrategies,
  stamp,
  index,
}: {
  adapter: NapCatAdapter;
  onChange: (a: NapCatAdapter) => void;
  onRemove?: () => void;
  strategies: Record<string, Strategy>;
  onStrategies: (s: Record<string, Strategy>) => void;
  stamp: React.ComponentProps<typeof Stamp>['state'];
  index: number;
}) {
  const [confirmRemove, setConfirmRemove] = useState(false);
  const patch = (p: Partial<NapCatAdapter>) => onChange({ ...adapter, ...p });

  const setGroups = (ids: string[]) => {
    patch({ allowed_group_ids: ids });
    // 移出名单的群，它的说话习惯也一起收起来
    const kept = Object.fromEntries(
      Object.entries(strategies).filter(
        ([id]) => ids.includes(id) || !adapter.allowed_group_ids.includes(id),
      ),
    );
    if (Object.keys(kept).length !== Object.keys(strategies).length) onStrategies(kept);
  };

  return (
    <Sheet
      className="envelope"
      title={
        <>
          {index === 0 ? '她的 QQ' : `第 ${index + 1} 个 QQ`}
          {adapter.qq_number && <span className="envelope__qq num">{adapter.qq_number}</span>}
        </>
      }
      aside={<Stamp state={adapter.enabled ? stamp : 'off'} />}
    >
      <Toggle
        on={adapter.enabled}
        onChange={(enabled) => patch({ enabled })}
        label="让她用这个 QQ 上线"
        sub="通过 NapCat（OneBot v11 正向 WebSocket）连接"
      />
      <div className="grid-2">
        <Field label="她的 QQ 号">
          <TextInput
            value={adapter.qq_number}
            onChange={(qq_number) => patch({ qq_number: qq_number.trim() })}
            inputMode="numeric"
            mono
          />
        </Field>
        <Field label="你的 QQ 号" hint="她会认得你是主人，可以用管理指令">
          <TextInput
            value={adapter.root}
            onChange={(root) => patch({ root: root.trim() })}
            inputMode="numeric"
            mono
          />
        </Field>
        <Field label="NapCat 地址">
          <TextInput
            value={adapter.ws_url}
            onChange={(ws_url) => patch({ ws_url: ws_url.trim() })}
            mono
            inputMode="url"
          />
        </Field>
        <Field label="NapCat 令牌">
          <TextInput value={adapter.token} onChange={(token) => patch({ token })} secret mono />
        </Field>
      </div>

      <div className="envelope__section">
        <Toggle
          on={adapter.enable_group_chat}
          onChange={(enable_group_chat) => patch({ enable_group_chat })}
          label="去群里聊天"
          sub="只会去下面列出的群；一个都不写就哪个群都不去"
        />
        {adapter.enable_group_chat && (
          <GroupList
            ids={adapter.allowed_group_ids}
            onChange={setGroups}
            strategies={strategies}
            onStrategies={onStrategies}
          />
        )}
      </div>

      <div className="envelope__section">
        <Toggle
          on={adapter.enable_private_chat}
          onChange={(enable_private_chat) => patch({ enable_private_chat })}
          label="接受私聊"
          sub="名单留空表示谁都可以私聊她"
        />
        {adapter.enable_private_chat && (
          <Chips
            values={adapter.allowed_private_user_ids}
            onChange={(allowed_private_user_ids) => patch({ allowed_private_user_ids })}
            validate={qqCheck}
            placeholder="只允许这些 QQ 私聊（可留空）"
          />
        )}
      </div>

      <Sheet title="和其他 AI 伙伴一起时" fold className="envelope__fold">
        <Field label="同在群里的 AI 伙伴的 QQ" hint="她们之间会轮流拿话筒，不会一起抢着说">
          <Chips
            values={adapter.peer_ai_ids}
            onChange={(peer_ai_ids) => patch({ peer_ai_ids })}
            validate={qqCheck}
            placeholder="QQ 号"
          />
        </Field>
        <Toggle
          on={adapter.group_dispatch_enabled}
          onChange={(group_dispatch_enabled) => patch({ group_dispatch_enabled })}
          label="参与群聊排班"
          sub="多位伙伴在同一个群时，由排班决定这句话谁来接"
        />
        {adapter.group_dispatch_enabled && (
          <div className="grid-2">
            <Field label="抢话筒的优先级" hint="越高越容易轮到她">
              <Stepper
                value={adapter.dispatch_priority}
                onChange={(dispatch_priority) => patch({ dispatch_priority })}
                min={-10}
                max={10}
                step={0.5}
                decimals={1}
              />
            </Field>
            <Field label="拿着话筒最多多久">
              <Stepper
                value={adapter.dispatch_lease_seconds}
                onChange={(dispatch_lease_seconds) => patch({ dispatch_lease_seconds })}
                min={5}
                max={3600}
                step={5}
                unit="秒"
              />
            </Field>
            <Field label="两次开口至少隔">
              <Stepper
                value={adapter.dispatch_min_reply_interval_seconds}
                onChange={(dispatch_min_reply_interval_seconds) =>
                  patch({ dispatch_min_reply_interval_seconds })
                }
                min={0}
                max={600}
                step={0.5}
                decimals={1}
                unit="秒"
              />
            </Field>
            <Field label="伙伴之间最多接几轮" hint="防止 AI 们自己聊个没完">
              <Stepper
                value={adapter.dispatch_max_peer_turns}
                onChange={(dispatch_max_peer_turns) => patch({ dispatch_max_peer_turns })}
                min={0}
                max={20}
                unit="轮"
              />
            </Field>
            <Field label="伙伴说完后等一等">
              <Stepper
                value={adapter.dispatch_peer_cooldown_seconds}
                onChange={(dispatch_peer_cooldown_seconds) => patch({ dispatch_peer_cooldown_seconds })}
                min={0}
                max={600}
                step={0.5}
                decimals={1}
                unit="秒"
              />
            </Field>
            <Field label="收集大家意愿的时间">
              <Stepper
                value={adapter.dispatch_score_collection_seconds}
                onChange={(dispatch_score_collection_seconds) => patch({ dispatch_score_collection_seconds })}
                min={0}
                max={10}
                step={0.05}
                decimals={2}
                unit="秒"
              />
            </Field>
            <Field label="活跃度统计窗口">
              <Stepper
                value={adapter.dispatch_activity_window_seconds}
                onChange={(dispatch_activity_window_seconds) => patch({ dispatch_activity_window_seconds })}
                min={0}
                max={86400}
                step={30}
                unit="秒"
              />
            </Field>
            <Field label="每说一句让一让" hint="说得多的伙伴下次让出一点机会">
              <Stepper
                value={adapter.dispatch_activity_penalty_per_reply}
                onChange={(dispatch_activity_penalty_per_reply) =>
                  patch({ dispatch_activity_penalty_per_reply })
                }
                min={0}
                max={1}
                step={0.01}
                decimals={2}
              />
            </Field>
            <Field label="最多让出多少">
              <Stepper
                value={adapter.dispatch_max_activity_penalty}
                onChange={(dispatch_max_activity_penalty) => patch({ dispatch_max_activity_penalty })}
                min={0}
                max={1}
                step={0.05}
                decimals={2}
              />
            </Field>
          </div>
        )}
        <Field label="排班记录放在哪" hint="留空用默认位置；多位伙伴要共用同一份才能互相排班">
          <TextInput
            value={adapter.dispatch_db_path}
            onChange={(dispatch_db_path) => patch({ dispatch_db_path })}
            mono
            placeholder="默认"
          />
        </Field>
      </Sheet>

      {onRemove && (
        <div className="envelope__remove">
          {confirmRemove ? (
            <>
              <span>确定不用这个 QQ 了？</span>
              <button className="soft-btn ghost" onClick={() => setConfirmRemove(false)}>
                算了
              </button>
              <button className="soft-btn blush" onClick={onRemove}>
                拆掉这封信
              </button>
            </>
          ) : (
            <button className="star-card__farewell" onClick={() => setConfirmRemove(true)}>
              不用这个 QQ 了…
            </button>
          )}
        </div>
      )}
    </Sheet>
  );
}

/** 群名单：每个群旁边可以选她在这个群里的说话习惯 */
function GroupList({
  ids,
  onChange,
  strategies,
  onStrategies,
}: {
  ids: string[];
  onChange: (ids: string[]) => void;
  strategies: Record<string, Strategy>;
  onStrategies: (s: Record<string, Strategy>) => void;
}) {
  const [text, setText] = useState('');
  const [problem, setProblem] = useState<string | null>(null);
  const add = () => {
    const id = text.trim();
    if (!id) return;
    const err = qqCheck(id);
    if (err) return setProblem(err);
    if (!ids.includes(id)) onChange([...ids, id]);
    setText('');
    setProblem(null);
  };
  return (
    <div className="groups">
      <AnimatePresence initial={false}>
        {ids.map((id) => (
          <motion.div
            key={id}
            className="groups__row"
            layout
            initial={{ opacity: 0, x: -10 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: 10 }}
          >
            <span className="groups__id num">{id}</span>
            <Segmented<Strategy>
              size="sm"
              value={strategies[id] ?? 'smart'}
              onChange={(v) => onStrategies({ ...strategies, [id]: v })}
              options={[
                { value: 'smart', label: '自己看着办' },
                { value: 'keyword', label: '叫到才说话' },
              ]}
            />
            <button
              className="groups__remove"
              aria-label={`移出群 ${id}`}
              onClick={() => onChange(ids.filter((x) => x !== id))}
            >
              ×
            </button>
          </motion.div>
        ))}
      </AnimatePresence>
      <div className="groups__add">
        <input
          className="input input--mono"
          value={text}
          placeholder="群号，回车添加"
          inputMode="numeric"
          onChange={(e) => {
            setText(e.target.value);
            setProblem(null);
          }}
          onKeyDown={(e) => e.key === 'Enter' && add()}
          onBlur={add}
        />
      </div>
      {problem && <span className="chips__error">{problem}</span>}
      <span className="field__hint">
        「叫到才说话」：只在消息命中她的名字或关键词时才回；「自己看着办」：她会判断要不要插话。
      </span>
    </div>
  );
}
