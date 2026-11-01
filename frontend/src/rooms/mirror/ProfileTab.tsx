import { queryClient, saveProfile, useProfile } from '@/lib/queries';
import type { Profile } from '@/lib/types';
import { Chips, Field, Letter, SaveWhisper, Sheet, TextInput } from '@/ui/form/fields';
import { useAutosave, useDraft } from '@/ui/form/useDraft';

export function ProfileTab({ persona }: { persona: string }) {
  const { data, isError, error, isFetchedAfterMount } = useProfile();
  const { draft, patch, baseline, setBaseline } = useDraft<Profile>(data, persona, isFetchedAfterMount);
  const autosave = useAutosave(draft, baseline, setBaseline, async (value) => {
    if (!value.name.trim()) throw new Error('名字不能空着');
    await saveProfile(persona, { ...value, name: value.name.trim() });
    queryClient.invalidateQueries({ queryKey: ['personas'] });
    queryClient.invalidateQueries({ queryKey: ['profile', persona] });
  });

  if (isError) return <p className="mirror__error">{(error as Error).message}</p>;
  if (!draft) return <p className="mirror__loading hand">正在找她的资料……</p>;
  const chars = draft.full_system_prompt.length;

  return (
    <>
      <Sheet title="名字" aside={<SaveWhisper {...autosave} />}>
        <div className="grid-2">
          <Field label="她叫什么" hint="会出现在大家眼前，也是她对自己的称呼">
            <TextInput value={draft.name} onChange={(name) => patch({ name })} />
          </Field>
          <Field label="门牌号" hint="她在 data/personas 下的文件夹，不能改">
            <TextInput value={persona} onChange={() => {}} readOnly mono />
          </Field>
        </div>
        <Field label="大家还怎么叫她" hint="群里有人这样叫，她就知道是在喊自己。回车或逗号分隔">
          <Chips
            values={draft.aliases}
            onChange={(aliases) => patch({ aliases })}
            placeholder="小月、月月……"
          />
        </Field>
      </Sheet>

      <Sheet
        title="写给她的信"
        aside={<span className="mirror__count num">约 {chars.toLocaleString('zh-CN')} 字</span>}
      >
        <p className="field__hint">
          这是她的完整人设：性格、说话的样子、和你的关系、不愿做的事……她每次开口前都会重读这封信。
        </p>
        <Letter
          value={draft.full_system_prompt}
          onChange={(full_system_prompt) => patch({ full_system_prompt })}
          placeholder={'亲爱的她：\n你是……'}
          minRows={14}
        />
      </Sheet>
    </>
  );
}
