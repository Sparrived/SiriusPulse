/** 内置小工具的「人话」名字；没收录的就显示原名 */
export const TOOL_NAMES: Record<string, string> = {
  autonomy: '自主时间',
  bash: '终端',
  desktop_screenshot: '截屏',
  group_file_exec: '群文件',
  image_generate: '画画',
  intend_pursue: '去做惦记的事',
  intend_share: '说出惦记的话',
  interaction_with_master: '和主人互动',
  napcat_call: '使唤 QQ',
  person_query: '想起某个人',
  person_update: '记住某个人',
  persona_notebook: '随身便签',
  read_image: '看图',
  read_skill: '翻技能书',
  visual_asset_manage: '表情包仓库',
  web_lookup: '上网查查',
  workflow_state: '工作流程',
};

export const toolName = (name: string) => TOOL_NAMES[name] ?? name;

const HUES = ['#ffd27a', '#ff9fb8', '#93a8ff', '#8fe0c0', '#c6a4ff', '#ffb98a', '#8ec5f0', '#f0a0c8'];

/** 每样东西一个固定的颜色 */
export function hueOf(name: string): string {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) | 0;
  return HUES[Math.abs(h) % HUES.length];
}
