# 用户档案记忆

该目录实现用户档案记忆，用于保存与具体用户相关的稳定事实、偏好、关系和摘要。

| 文件 | 说明 |
|---|---|
| `models.py` | `ProfileItem`、`ProfileSection`、`ProfileUpdate`、`UserPersonaProfile`。 |
| `store.py` | 用户档案持久化。 |
| `manager.py` | 档案读写、合并和更新逻辑。 |
| `prompt.py` | 将档案渲染为 Prompt 片段。 |
