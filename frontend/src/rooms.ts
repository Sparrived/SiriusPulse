import type { ComponentType, SVGProps } from 'react';
import {
  AtticIcon,
  BagIcon,
  DiaryIcon,
  HomeIcon,
  JarIcon,
  LedgerIcon,
  MailIcon,
  MirrorIcon,
  StarsIcon,
  WeatherIcon,
} from './ui/icons';

export interface Room {
  id: string;
  path: string;
  name: string;
  /** 一句话说明，同时是命令面板的搜索词 */
  hint: string;
  Icon: ComponentType<SVGProps<SVGSVGElement>>;
  /** 搬进新家之前，先指向旧版控制台里对应的页面 */
  legacy?: { page: string; label: string }[];
  ready?: boolean;
}

export const ROOMS: Room[] = [
  { id: 'home', path: '/', name: '客厅', hint: '她在这里 · 此刻的心情和动静', Icon: HomeIcon, ready: true },
  {
    id: 'stars',
    path: '/stars',
    name: '星图',
    hint: '伙伴们 · 迎接新伙伴',
    Icon: StarsIcon,
    legacy: [
      { page: 'create-persona', label: '新建人格' },
      { page: 'dashboard', label: '人格总览' },
    ],
  },
  {
    id: 'mirror',
    path: '/mirror',
    name: '镜子',
    hint: '她的样子 · 人设与性格调音',
    Icon: MirrorIcon,
    legacy: [
      { page: 'persona', label: '人格配置' },
      { page: 'experience', label: '体验参数' },
    ],
  },
  {
    id: 'diary',
    path: '/diary',
    name: '日记',
    hint: '她的一天 · 对话、自主行为、工作记录',
    Icon: DiaryIcon,
    legacy: [
      { page: 'conversation-history', label: '对话分析' },
      { page: 'autonomy', label: '自主行为' },
      { page: 'work-mode', label: '工作模式' },
    ],
  },
  {
    id: 'memories',
    path: '/memories',
    name: '回忆',
    hint: '记忆瓶 · 她记住的事',
    Icon: JarIcon,
    legacy: [{ page: 'memory-viz', label: '记忆管理' }],
  },
  {
    id: 'heart',
    path: '/heart',
    name: '心事',
    hint: '情绪天气 · 她怎么感受大家',
    Icon: WeatherIcon,
    legacy: [{ page: 'cognition', label: '认知分析' }],
  },
  {
    id: 'bag',
    path: '/bag',
    name: '背包',
    hint: '本领 · 工具、MCP、插件',
    Icon: BagIcon,
    legacy: [
      { page: 'tools', label: 'Tools 与 MCP' },
      { page: 'plugins', label: '插件' },
      { page: 'tools-tracker', label: 'Tool 追踪' },
    ],
  },
  {
    id: 'mailbox',
    path: '/mailbox',
    name: '信箱',
    hint: '联络方式 · QQ、账户、群聊排班',
    Icon: MailIcon,
    legacy: [
      { page: 'adapters', label: 'Adapter' },
      { page: 'accounts', label: '人格账户' },
      { page: 'dispatcher', label: '群聊调度' },
    ],
  },
  {
    id: 'ledger',
    path: '/ledger',
    name: '小账本',
    hint: '她吃了多少星光 · Token 用量',
    Icon: LedgerIcon,
    legacy: [{ page: 'token-tracker', label: 'Token 追踪' }],
  },
  {
    id: 'attic',
    path: '/attic',
    name: '阁楼',
    hint: '维护 · 全局设置、AMKR、日志',
    Icon: AtticIcon,
    legacy: [
      { page: 'global-settings', label: '全局设置' },
      { page: 'amkr', label: 'AMKR 运维' },
      { page: 'logs', label: '实时日志' },
    ],
  },
];

export function roomByPath(path: string): Room | undefined {
  return ROOMS.find((r) => r.path === path);
}
