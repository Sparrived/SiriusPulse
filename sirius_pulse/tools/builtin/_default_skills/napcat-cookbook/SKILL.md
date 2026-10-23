---
name: napcat-cookbook
description: NapCat / OneBot v11 接口速查。调用 napcat_call 之前用这里核对 action 名称、参数字段与返回结构。做群成员查询、群管理（禁言、踢人、改名片）、点赞、撤回消息，或需要 OneBot 高级接口（群公告、精华消息、戳一戳、合并转发、文件上传）时使用。
---

# NapCat Cookbook

`napcat_call` 是访问 NapCat（OneBot v11）的唯一入口。不要为这些操作去用 `bash`、
`curl`、WebSocket 脚本或搜索容器配置——那些路径既绕过了权限检查，也拿不到适配器状态。

## 调用形态

```json
{"action": "get_group_member_list", "params": {"group_id": 123456789}}
```

也可以把参数平铺在顶层，工具会把它们合并进 `params`：

```json
{"action": "set_group_ban", "user_id": 10001, "duration": 600}
```

`params` 也可以是一段 JSON 字符串，但优先用对象更不容易出错。字符串形式的数字
（`"10001"`、`"600"`）会被自动转成整数，`qq_10001` 这种内部写法也会被归一化。

## 工具替你做的事

- **自动注入群号**：`get_group_*` / `set_group_*` 类 action 没给 `group_id` 时，用当前群聊的群号。
- **别名**：`kick` → `set_group_kick`，`mute` / `mute_member` → `set_group_ban`，
  `mute_all` / `whole_ban` → `set_group_whole_ban`，`like` → `send_like`，
  `list_members` → `get_group_member_list`，`get_member` → `get_group_member_info`。
- **权限**：管理动作（`set_group_ban`、`set_group_whole_ban`、`set_group_kick`、`set_group_card`、
  `set_group_name`、`set_group_admin`、`set_group_special_title`、`delete_msg`）只允许开发者/管理员执行，
  其他调用者会收到 `权限不足` 的失败结果。被拒绝时不要换别的工具去绕过。
- **返回**：`get_group_member_list` / `get_group_member_info` 会整理成人可读文本；
  其他 action 返回执行摘要，原始响应在 `data` 里。

OneBot 的原始响应形如 `{"status": "ok", "retcode": 0, "data": ...}`，失败时适配器会抛出异常，
工具会转成 `success=false` 并带上错误原因。

## 1. 成员与群信息查询

| action | 参数 | 说明 |
|---|---|---|
| `get_group_member_list` | `group_id`, `no_cache`(可选) | 群成员列表，返回 `user_id`/`nickname`/`card`/`role`/`title` 等。工具只展示前 80 人 |
| `get_group_member_info` | `group_id`, `user_id`, `no_cache`(可选) | 单个成员的详细信息 |
| `get_group_info` | `group_id`, `no_cache`(可选) | 群名、人数、群主等群资料 |
| `get_group_list` | 无 | 机器人已加入的群列表 |
| `get_group_honor_info` | `group_id`, `type`（`talkative`/`performer`/`legend`/`strong_newbie`/`emotion`/`all`） | 群荣誉：龙王、群聊之火等 |
| `get_stranger_info` | `user_id`, `no_cache`(可选) | 陌生人资料（昵称、性别、年龄） |
| `get_friend_list` | 无 | 好友列表 |
| `get_login_info` | 无 | 当前登录账号的 QQ 号与昵称 |
| `get_status` | 无 | 适配器在线状态 |
| `get_group_msg_history` | `group_id`, `count`, `message_seq`(可选) | 群历史消息 |
| `get_group_file_list` | `group_id` | 群文件列表 |
| `get_group_at_all_remain` | `group_id` | 剩余 @全体成员 次数 |

```json
{"action": "get_group_honor_info", "params": {"group_id": 123456789, "type": "talkative"}}
```

```json
{"action": "get_group_member_info", "params": {"group_id": 123456789, "user_id": 10001}}
```

## 2. 群管理操作

| action | 参数 | 说明 |
|---|---|---|
| `set_group_ban` | `group_id`, `user_id`, `duration`（秒，`0` = 解除） | 单人禁言。上限通常 30 天：`2592000` |
| `set_group_whole_ban` | `group_id`, `enable`（bool） | 全员禁言开关 |
| `set_group_kick` | `group_id`, `user_id`, `reject_add_request`(可选 bool) | 移出群；`reject_add_request=true` 同时拒绝再次加群 |
| `set_group_card` | `group_id`, `user_id`, `card` | 改群名片；`card=""` 表示清空 |
| `set_group_name` | `group_id`, `group_name` | 改群名 |
| `set_group_admin` | `group_id`, `user_id`, `enable`（bool） | 设置/取消管理员 |
| `set_group_special_title` | `group_id`, `user_id`, `special_title` | 设置专属头衔 |
| `delete_msg` | `message_id` | 撤回消息（不需要群号） |
| `set_group_leave` | `group_id`, `is_dismiss`(可选 bool) | 退群；`is_dismiss=true` 为解散群（群主） |
| `set_group_add_request` | `flag`, `approve`(bool), `reason`(可选) | 处理加群请求 |
| `set_friend_add_request` | `flag`, `approve`(bool), `remark`(可选) | 处理加好友请求 |
| `send_group_notice` | `group_id`, `content`, `image`(可选) | 发群公告 |
| `_get_group_notice` | `group_id` | 读群公告 |
| `set_essence_msg` | `message_id` | 设为精华消息 |
| `delete_essence_msg` | `message_id` | 取消精华消息 |
| `get_essence_msg_list` | `group_id` | 精华消息列表 |

```json
{"action": "set_group_ban", "params": {"user_id": 10001, "duration": 600}}
```

```json
{"action": "set_group_kick", "params": {"user_id": 10001, "reject_add_request": false}}
```

```json
{"action": "delete_msg", "params": {"message_id": 987654321}}
```

管理动作前先确认三件事：**对象**（真实群号/QQ 号/消息 ID）、**参数**（时长、开关、文案）、
**诉求**（对方确实要求做这件事，而不是抱怨）。缺一项就先用 `send_midway_msg` 问清楚。

## 3. 互动与社交

| action | 参数 | 说明 |
|---|---|---|
| `send_like` | `user_id`, `times`（1-10） | 给 QQ 用户点赞。工具会把 `times` 限制在 1-10 |
| `friend_poke` | `user_id` | 戳一戳好友 |
| `group_poke` | `group_id`, `user_id` | 戳一戳群成员 |
| `send_private_msg` | `user_id`, `message` | 私聊发消息 |
| `send_group_msg` | `group_id`, `message` | 群聊发消息 |
| `send_msg` | `message_type`(`group`/`private`), `group_id`/`user_id`, `message` | 通用发送 |
| `send_group_forward_msg` | `group_id`, `messages` | 合并转发到群 |
| `send_private_forward_msg` | `user_id`, `messages` | 合并转发到私聊 |
| `upload_group_file` | `group_id`, `file`, `name`, `folder`(可选) | 上传群文件 |
| `upload_private_file` | `user_id`, `file`, `name` | 上传私聊文件 |

```json
{"action": "send_like", "params": {"user_id": 10001, "times": 3}}
```

用户说“再来一次”“继续上次点赞”而没有新目标时：

```json
{"action": "send_like", "params": {"reuse_last": true}}
```

上一次成功点赞的目标会按聊天会话记住；失败的调用不会覆盖它。

**发言类 action（`send_group_msg` / `send_private_msg` / 转发）不要在聊天回合里用来代替正常回复。**
正常的对外回复交给正文；只有工作模式里需要中途对外说话时才用 `send_midway_msg`。

## 4. 使用规范与安全原则

1. **一次一个动作**。先调用，读结果，再决定下一步；不要凭猜测并行堆叠多个管理动作。
2. **不猜测 ID**。群号/QQ 号/消息 ID 必须来自当前上下文、`get_group_member_list` 或历史消息。
3. **不可逆动作先确认**。踢人、退群、解散、全员禁言、撤回他人消息属于不可逆操作，
   对象或参数有任何不确定就先向对方确认。
4. **权限不足不绕行**。收到 `权限不足` 就如实反馈，不要改用 `bash` 或伪造结果。
5. **不暴露内部细节**。不要把原始 JSON 响应、接口名堆砌或适配器配置直接念给群里听；
   把结果翻译成自然语言。
6. **失败就说明**。`success=false` 时把 `error` 里的原因讲清楚，不要编造“已完成”。
7. **没有对应 action 就不要发明**。拿不准的 action 名先在这个文档里核对，
   或用 `get_group_list` / `get_status` 之类只读接口探明环境。
