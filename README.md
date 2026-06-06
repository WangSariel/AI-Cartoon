# AI-Cartoon

AI-Cartoon 是一个面向小说创作与漫画分镜生成的 AI 工作台。它支持通过对话生成小说内容，将章节自动拆分为漫画分镜，并调用 OpenAI-compatible 图片接口生成漫画页面。

这个版本在保留原有核心能力的基础上，重点优化了前端视觉、移动端体验、图片生成稳定性、本地 SQLite 运行体验和项目文档结构，更适合作为个人 fork 后继续扩展。

## 功能亮点

- AI 对话创作：围绕章节进行连续对话，辅助生成小说正文。
- 小说导入：支持粘贴已有小说内容，再生成漫画分镜。
- 自动分镜：根据章节内容生成可编辑的漫画页脚本。
- 漫画生成：支持整章生成、单张重绘、图片预览和下载。
- 角色一致性：提供角色外貌卡、设定组、多张参考图和章节继承机制。
- 清新简约 UI：更柔和的配色、留白、圆角、阴影、空状态和错误提示。
- 暗黑模式：前端支持浅色/暗色主题切换。
- 稳定性增强：图片 API 支持超时、重试、指数退避、备用 endpoint 和更友好的进度反馈。
- 本地优先：默认使用 SQLite，开箱即可跑，不强依赖 PostgreSQL。
- 导入导出：支持作品包导入导出，方便迁移和备份。

## 技术栈

前端：

- React
- TypeScript
- Vite
- Tailwind CSS
- lucide-react

后端：

- Python
- FastAPI
- SQLAlchemy
- SQLite 默认
- PostgreSQL 可选

AI 服务：

- DeepSeek API：用于对话、小说生成和分镜生成
- OpenAI-compatible Image API：用于漫画图片生成，默认配置为 AIHubMix

## 项目结构

```text
AI-Cartoon/
├─ backend/              # FastAPI 后端
│  ├─ main.py            # API 路由与 SSE 生成流程
│  ├─ config.py          # 环境变量配置
│  ├─ database.py        # 数据库连接与初始化
│  ├─ models.py          # SQLAlchemy 模型
│  ├─ schemas.py         # API schema
│  └─ services/          # DeepSeek / 图片生成服务
├─ frontend/             # React 前端
│  ├─ src/
│  │  ├─ components/     # 页面与功能组件
│  │  ├─ api.ts          # 前后端请求封装
│  │  └─ index.css       # 全局主题与样式
│  └─ package.json
├─ install.bat           # Windows 一键安装依赖
├─ start.bat             # Windows 一键启动
└─ README.md
```

## 快速开始

### 1. 安装依赖

Windows 推荐直接运行：

```powershell
.\install.bat
```

也可以手动安装：

```powershell
cd backend
pip install -r requirements.txt

cd ..\frontend
npm install
```

### 2. 配置环境变量

复制后端环境变量示例：

```powershell
copy backend\.env.example backend\.env
```

常用配置如下：

```env
# Database
DATABASE_URL=sqlite:///./manga.db

# DeepSeek
DEEPSEEK_API_KEY=

# Image API
IMAGE_API_KEY=
IMAGE_API_BASE_URL=https://aihubmix.com/v1
IMAGE_API_BASE_URL_FALLBACK=
IMAGE_MODEL=gpt-image-2
IMAGE_SIZE=1024x1536
IMAGE_MAX_RETRIES=6
IMAGE_REQUEST_TIMEOUT_SECONDS=300

# Server
HOST=127.0.0.1
PORT=8000
```

说明：

- `DEEPSEEK_API_KEY` 和 `IMAGE_API_KEY` 可以写在 `backend/.env`，也可以在网页右下角的 API Key 设置中填写。
- `backend/.env` 不会被 Git 提交，请不要把真实密钥写入 README 或公开仓库。
- 默认数据库是 SQLite，后端启动后会自动创建 `backend/manga.db`。

### 3. 启动项目

推荐使用：

```powershell
.\start.bat
```

或手动分别启动后端和前端：

```powershell
cd backend
python main.py
```

另开一个终端：

```powershell
cd frontend
npm.cmd run dev -- --host 127.0.0.1
```

访问：

```text
http://localhost:5173
```

## 图片生成配置

当前默认图片接口：

```env
IMAGE_API_BASE_URL=https://aihubmix.com/v1
IMAGE_MODEL=gpt-image-2
```

如果你的图片服务不稳定，可以配置备用 endpoint：

```env
IMAGE_API_BASE_URL=https://aihubmix.com/v1
IMAGE_API_BASE_URL_FALLBACK=https://your-openai-compatible-proxy.example/v1
```

备用 endpoint 必须兼容以下接口：

```text
POST /v1/images/generations
POST /v1/images/edits
```

图片生成已内置：

- 连接池复用
- 300 秒请求超时
- 指数退避重试
- 随机抖动
- 429 / 5xx / 连接断开重试
- 401 / 402 / 403 / 404 等错误即时返回
- SSE 进度反馈：等待、重试、下载、完成、失败

## SQLite 与 PostgreSQL

本地开发默认使用 SQLite：

```env
DATABASE_URL=sqlite:///./manga.db
```

如果你想切换到 PostgreSQL，可以启动 Docker Compose 中的数据库：

```powershell
docker compose up -d db
```

然后修改 `backend/.env`：

```env
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/manga_novel
```

如果 5432 端口被占用，可以修改 `docker-compose.yml` 的端口映射，例如：

```yaml
ports:
  - "5433:5432"
```

然后同步修改：

```env
DATABASE_URL=postgresql://postgres:postgres@localhost:5433/manga_novel
```

## 常用命令

前端构建：

```powershell
cd frontend
npm.cmd run build
```

前端开发服务：

```powershell
cd frontend
npm.cmd run dev -- --host 127.0.0.1
```

后端启动：

```powershell
cd backend
python main.py
```

后端语法检查：

```powershell
python -m py_compile backend\config.py backend\main.py backend\services\image2.py
```

## 测试 Checklist

- 可以打开首页并切换浅色/暗色主题。
- 可以创建作品和章节。
- 可以完成 AI 对话并生成小说正文。
- 可以生成并编辑漫画分镜。
- 可以上传角色参考图。
- 可以生成漫画图片，并看到等待、重试、下载等进度状态。
- 图片生成失败时，错误提示应为柔和提示框，不应出现刺眼红色警示块。
- 可以导出作品包。
- 重启后端后，SQLite 数据仍能正常读取。

## 安全说明

请不要提交以下文件：

- `backend/.env`
- `backend/manga.db`
- `backend/manga_outputs/`
- `frontend/node_modules/`
- `frontend/dist/`

这些文件已在 `.gitignore` 中排除。

## Fork 说明

这是基于原项目思路继续改造的个人升级版本，重点增强了视觉设计、稳定性、本地运行体验和工程配置。继续二次开发时，请保留原项目许可证要求，并根据你的仓库情况补充 LICENSE、贡献说明和部署文档。

## License

MIT
