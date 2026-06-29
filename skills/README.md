# 外部 Skills 目录

把用户自定义 Skill 放在此目录。框架会在运行时扫描该目录，并与内置 Skill 一起注册。

```python
from sirius_pulse.skills.models import SkillResult

SKILL_META = {
    "name": "my_skill",
    "description": "说明模型什么时候应该调用这个工具。",
    "parameters": [],
}

async def run(**kwargs):
    return SkillResult.ok(text="完成")
```

不要在 Skill 源码中硬编码 API Key；文件、网络、系统和群管理能力默认不要开放给所有人。
