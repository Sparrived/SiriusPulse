import { queryClient, saveExperience, useExperience } from '@/lib/queries';
import type { Experience } from '@/lib/types';
import { Chips, Field, SaveWhisper, Sheet, Stepper, Toggle } from '@/ui/form/fields';
import { useAutosave, useDraft } from '@/ui/form/useDraft';
import { PersonalityPad } from './PersonalityPad';

/** 镜子里只管这些字段；群级回复策略在信箱里改，两边互不覆盖 */
export function pickMirrorExperience(e: Experience): Partial<Experience> {
  const { group_reply_strategies: _skip, ...rest } = e;
  return rest;
}

export function useExperienceDraft(persona: string) {
  const { data, isError, error, isFetchedAfterMount } = useExperience();
  const draft = useDraft<Experience>(data, persona, isFetchedAfterMount);
  const autosave = useAutosave(draft.draft, draft.baseline, draft.setBaseline, async (value) => {
    await saveExperience(persona, pickMirrorExperience(value));
    queryClient.invalidateQueries({ queryKey: ['experience', persona] });
  });
  return { ...draft, autosave, isError, error };
}

export function PersonalityTab({ persona }: { persona: string }) {
  const { draft, patch, autosave, isError, error } = useExperienceDraft(persona);
  if (isError) return <p className="mirror__error">{(error as Error).message}</p>;
  if (!draft) return <p className="mirror__loading hand">正在找她的性子……</p>;

  return (
    <>
      <Sheet title="她的性子" aside={<SaveWhisper {...autosave} />}>
        <PersonalityPad
          expressiveness={draft.expressiveness}
          engagement={draft.engagement_sensitivity}
          onChange={patch}
        />
        <Field label="一句话最多说几个字" hint="超过就分成几条发；越小越像在打字聊天">
          <Stepper
            value={draft.max_sentence_chars}
            onChange={(max_sentence_chars) => patch({ max_sentence_chars })}
            min={5}
            max={500}
            step={5}
            unit="字"
          />
        </Field>
      </Sheet>

      <Sheet title="小工具" fold>
        <Toggle
          on={draft.enable_tools}
          onChange={(enable_tools) => patch({ enable_tools })}
          label="允许她用小工具"
          sub="查天气、搜网页、画画……具体有哪些在背包里"
        />
        {draft.enable_tools && (
          <>
            <Field label="一次最多连着用几轮">
              <Stepper
                value={draft.max_tool_rounds}
                onChange={(max_tool_rounds) => patch({ max_tool_rounds })}
                min={1}
                max={20}
                unit="轮"
              />
            </Field>
            <Toggle
              on={draft.auto_install_tool_deps}
              onChange={(auto_install_tool_deps) => patch({ auto_install_tool_deps })}
              label="缺零件时自动装上"
              sub="工具需要的 Python 依赖不存在时自动安装"
            />
          </>
        )}
      </Sheet>

      <Sheet title="回忆" fold>
        <div className="grid-2">
          <Field label="每次想起几件事" hint="回话前从记忆里翻出最相关的几件">
            <Stepper
              value={draft.memory_unit_top_k}
              onChange={(memory_unit_top_k) => patch({ memory_unit_top_k })}
              min={0}
              max={50}
              unit="件"
            />
          </Field>
          <Field label="回忆最多占多少篇幅">
            <Stepper
              value={draft.memory_unit_token_budget}
              onChange={(memory_unit_token_budget) => patch({ memory_unit_token_budget })}
              min={0}
              max={200_000}
              step={1000}
              unit="tokens"
            />
          </Field>
        </div>
        <Field label="随身便签的篇幅" hint="每个聊天里她自己记的小纸条；0 表示不带便签">
          <Stepper
            value={draft.persona_notebook_token_budget}
            onChange={(persona_notebook_token_budget) => patch({ persona_notebook_token_budget })}
            min={0}
            max={32_000}
            step={256}
            unit="tokens"
          />
        </Field>
      </Sheet>

      <Sheet title="群里的规矩" fold>
        <Field label="群里还有哪些 AI" hint="写上它们的名字，她就不会跟它们抢话，也不会把它们当成人">
          <Chips
            values={draft.other_ai_names}
            onChange={(other_ai_names) => patch({ other_ai_names })}
            placeholder="名字，回车添加"
          />
        </Field>
        <Field label="以这些开头的消息她不看" hint="比如别的机器人的指令前缀">
          <Chips
            values={draft.message_prefixes}
            onChange={(message_prefixes) => patch({ message_prefixes })}
            placeholder="例如 . 或 /"
          />
        </Field>
      </Sheet>
    </>
  );
}
