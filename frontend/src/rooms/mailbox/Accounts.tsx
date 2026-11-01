import { AnimatePresence, motion } from 'motion/react';
import { queryClient, saveAccounts, useAccounts } from '@/lib/queries';
import type { Account } from '@/lib/types';
import { Field, SaveWhisper, Segmented, Sheet, TextInput } from '@/ui/form/fields';
import { useAutosave, useDraft } from '@/ui/form/useDraft';

const KIND_LABEL: Record<string, string> = { github: 'GitHub' };

/** 她自己的服务账号（目前是 GitHub）：凭据只回显掩码，原样提交即保留 */
export function Accounts({ persona }: { persona: string }) {
  const { data, isFetchedAfterMount, isError, error } = useAccounts();
  const server = data ? { accounts: data.accounts } : undefined;
  const { draft, setDraft, baseline, setBaseline } = useDraft<{ accounts: Account[] }>(
    server,
    persona,
    isFetchedAfterMount,
  );
  const autosave = useAutosave(draft, baseline, setBaseline, async (value) => {
    // 刚添加、还什么都没填的空行先不提交；填了东西却没起名字才提醒
    const filled = value.accounts.filter((a) => a.name.trim() || a.username.trim() || a.secret);
    if (filled.some((a) => !a.name.trim())) throw new Error('每个账号都要起个名字');
    await saveAccounts(persona, filled);
    queryClient.invalidateQueries({ queryKey: ['accounts', persona] });
  });

  if (isError) return null;
  const kinds = data?.kinds ?? ['github'];
  const accounts = draft?.accounts ?? [];
  const update = (i: number, p: Partial<Account>) =>
    setDraft({ accounts: accounts.map((a, j) => (j === i ? { ...a, ...p } : a)) });

  return (
    <Sheet title="她自己的账号" aside={<SaveWhisper {...autosave} />} fold={!accounts.length}>
      <p className="field__hint">
        让她用自己的身份去做事（比如以她的 GitHub 账号提
        issue）。令牌保存后只显示掩码，不改就不会动它；清空就是删除。
        {error ? ` ${(error as Error).message}` : ''}
      </p>
      <AnimatePresence initial={false}>
        {accounts.map((a, i) => (
          <motion.div
            key={i}
            className="account"
            layout
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, height: 0 }}
          >
            <div className="account__head">
              <Segmented
                size="sm"
                value={a.kind}
                onChange={(kind) => update(i, { kind })}
                options={kinds.map((k) => ({ value: k, label: KIND_LABEL[k] ?? k }))}
              />
              <button
                className="groups__remove"
                aria-label="删除这个账号"
                onClick={() => setDraft({ accounts: accounts.filter((_, j) => j !== i) })}
              >
                ×
              </button>
            </div>
            <div className="grid-2">
              <Field label="叫它什么" hint="同一类账号里不能重名">
                <TextInput value={a.name} onChange={(name) => update(i, { name })} />
              </Field>
              <Field label="用户名">
                <TextInput value={a.username} onChange={(username) => update(i, { username })} mono />
              </Field>
              <Field label="令牌">
                <TextInput value={a.secret} onChange={(secret) => update(i, { secret })} secret mono />
              </Field>
              <Field label="放进哪个环境变量" hint="工具运行时从这里读取">
                <TextInput
                  value={a.env}
                  onChange={(env) => update(i, { env })}
                  mono
                  placeholder={data?.default_env[a.kind] ?? ''}
                />
              </Field>
            </div>
          </motion.div>
        ))}
      </AnimatePresence>
      <button
        className="soft-btn ghost account__add"
        onClick={() =>
          setDraft({
            accounts: [...accounts, { kind: kinds[0], name: '', username: '', secret: '', env: '' }],
          })
        }
      >
        + 给她添一个账号
      </button>
    </Sheet>
  );
}
