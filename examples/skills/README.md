# skills 目录说明

此目录用于存放 Sirius Pulse 在当前 work_path 下可自动发现的外部 SKILL 文件。

- 每个 SKILL 使用单独的 Python 文件。
- 文件需导出 SKILL_META 字典和 run() 函数。
- 文件名建议使用英文、数字、下划线，避免以下划线开头。
- 框架会自动创建并扫描此目录；若显式关闭 SKILL，目录仍会保留，便于后续直接添加文件。

最小示例：

```python
SKILL_META = {
    "name": "hello_skill",
    "description": "返回简单问候语",
    "parameters": {
        "name": {
            "type": "str",
            "description": "要问候的名字",
            "required": True,
        }
    },
}


def run(name: str, **kwargs):
    return {"message": f"你好，{name}"}
```
