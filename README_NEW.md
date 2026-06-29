# Sirius Pulse

Sirius Pulse 是一个本地优先的异步角色扮演聊天框架，面向多人格、多模型、多平台和长期记忆场景。它内置 WebUI 管理面板、QQ / NapCat OneBot v11 适配器、模型 Provider 路由、分层记忆、模型可调用 Skills 和用户命令 Plugins。

> 这是新版 README 草案；保留现有 `README.md` 不变。

## 主要能力

- 多人格运行：每个人格独立配置、独立记忆、独立子进程运行。
- WebUI 管理：管理人格、Provider、模型编排、平台适配器、Skills、Plugins、日志、Token 和记忆。
- 多 Provider：支持 OpenAI-compatible、DeepSeek、SiliconFlow、阿里云百炼、智谱、火山、Mimo、YTea、Mock 等实现。
- QQ 接入：通过 NapCat OneBot v11 WebSocket 接入群聊和私聊。
- 长期记忆：基础记忆、语义画像、日记、记忆单元、术语表、用户档案和认知事件。
- 双扩展系统：Skills 给模型调用工具；Plugins 给用户触发命令。

## 快速开始

```bash
git clone https://github.com/Sparrived/SiriusChat.git
cd SiriusChat
python -m venv .venv
.venv\Scripts\activate
pip install -e .
python main.py webui
```

打开 `http://127.0.0.1:8080`。

## CLI

```bash
python main.py run
python main.py webui
python main.py webui --status
python main.py webui --stop
python main.py assistant --butler ws://127.0.0.1:9000
python main.py persona list
python main.py persona create <name>
python main.py persona activate <name>
python main.py persona delete <name> --force
```

## 项目结构

```text
sirius_pulse/
├── adapters/      # 平台无关消息模型和适配器抽象
├── config/        # 配置模型、JSON/JSONC 读写和默认配置
├── core/          # 对话引擎、Brain、Pipeline、Prompt、事件和后台任务
├── memory/        # 基础记忆、语义画像、日记、记忆单元、用户档案、术语
├── platforms/     # NapCat OneBot v11 平台实现
├── plugins/       # Plugin 框架
├── providers/     # LLM Provider 实现与路由
├── skills/        # Skill 框架和内置 Skill
└── webui/         # aiohttp API 和静态管理面板
```

## 文档

```bash
cd docs
npm install
npm run dev
```

## 安全提醒

不要提交 API Key、Cookie、QQ 账号敏感信息或私有聊天数据。群管理、桌面截图、工作区文件和 Web 查询类 Skill 应按需启用。

## License

MIT
