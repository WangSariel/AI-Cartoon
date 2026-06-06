# AI-Cartoon

AI-Cartoon 是一个 AI 小说创作与漫画生成工作台。你可以通过对话生成小说内容，自动拆分漫画分镜，并调用图片生成服务输出漫画页面。

## 功能亮点

- AI 小说对话创作与已有小说粘贴导入
- 自动生成并可编辑漫画分镜
- 漫画图片生成、单张重绘和图片预览
- 角色外貌卡、设定组和多张参考图
- 清新简约前端主题与暗黑模式
- 作品导入/导出，便于备份和迁移

## 技术栈

- 前端：React / TypeScript / Vite / TailwindCSS
- 后端：Python / FastAPI / SQLAlchemy / SQLite（默认）/ PostgreSQL（可选）
- AI 服务：DeepSeek API + OpenAI-compatible 图片 API

## 快速启动

### 1. 安装依赖

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

### 2. 数据库

本地试运行默认使用 SQLite，不需要安装 PostgreSQL 或 Docker。

默认连接为：

```env
DATABASE_URL=sqlite:///./manga.db
```

后端启动后会自动在 `backend/manga.db` 创建数据库文件。

如果你想切回 PostgreSQL，请确保：

- PostgreSQL 服务已经启动
- 已创建数据库 `manga_novel`
- `backend/.env` 中的 `DATABASE_URL` 与你的用户名、密码、端口、数据库名一致

创建数据库示例：

```sql
CREATE DATABASE manga_novel;
```

### 3. 配置环境变量

如果 `backend/.env` 不存在，可以从示例复制：

```powershell
copy backend\.env.example backend\.env
```

常用配置：

```env
DATABASE_URL=sqlite:///./manga.db
IMAGE_API_BASE_URL=https://aihubmix.com/v1
IMAGE_API_BASE_URL_FALLBACK=
IMAGE_MODEL=gpt-image-2
IMAGE_MAX_RETRIES=6
IMAGE_REQUEST_TIMEOUT_SECONDS=300
HOST=127.0.0.1
PORT=8000
```

DeepSeek 和图片 API Key 也可以直接在网页右下角的 API Key 设置里填写。

### 4. 启动应用

```powershell
.\start.bat
```

或分别启动：

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

## 切换回 PostgreSQL（可选）

如果你之后想使用 PostgreSQL，可以选择 Docker Compose：

```powershell
docker compose up -d db
```

然后把 `backend/.env` 改成：

```env
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/manga_novel
```

如果后端启动时报 `connection refused`、`password authentication failed`、`database "manga_novel" does not exist`：

1. 确认数据库服务正在运行：

```powershell
docker compose ps
```

2. 如果没有运行，启动数据库：

```powershell
docker compose up -d db
```

3. 确认 `backend/.env` 中的连接串：

```env
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/manga_novel
```

4. 如果 5432 端口已被本机 PostgreSQL 占用，可以改 `docker-compose.yml` 端口，例如：

```yaml
ports:
  - "5433:5432"
```

然后同步修改：

```env
DATABASE_URL=postgresql://postgres:postgres@localhost:5433/manga_novel
```

## License

MIT
