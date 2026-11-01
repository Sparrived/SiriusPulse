/** 记忆的种类：瓶子里萤火的颜色、列表里的小标签 */
export const MEMORY_KINDS: { value: string; label: string; color: string }[] = [
  { value: 'event', label: '发生的事', color: '#ffd27a' },
  { value: 'preference', label: '喜好', color: '#ff9fb8' },
  { value: 'relationship', label: '关系', color: '#ff7aa2' },
  { value: 'boundary', label: '边界', color: '#93a8ff' },
  { value: 'group_norm', label: '群里的规矩', color: '#8fe0c0' },
  { value: 'term', label: '黑话', color: '#c6a4ff' },
  { value: 'profile_candidate', label: '关于某人', color: '#ffb98a' },
  { value: 'note', label: '小纸条', color: '#fff1a8' },
];

const BY_VALUE = new Map(MEMORY_KINDS.map((k) => [k.value, k]));

export const memoryColor = (type: string) => BY_VALUE.get(type)?.color ?? '#e8ecff';
export const memoryLabel = (type: string) => BY_VALUE.get(type)?.label ?? type;

export const LIFESPAN = [
  { value: 'short', label: '很快会淡忘' },
  { value: 'medium', label: '记一阵子' },
  { value: 'long', label: '一直记得' },
];

export const SCOPES = [
  { value: 'group', label: '整个群' },
  { value: 'user', label: '某个人' },
  { value: 'global', label: '到哪都记得' },
];
