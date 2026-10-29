# 课堂前端（classroom-webapp）

MAT「课堂」学生端界面：Vue 3 + TypeScript + Vite。`src/views` 里的 6 个页面
（落地页 / 入口 / 我的课堂 / 课程详情 / 生成课堂 / 小节学习）复刻自旧仓库
`Multi-agent-Eduacation-System/frontend/src`，另外 3 页（单元大纲、课后小结、课堂深链跳转）
承接后端独有能力。

数据层只认真实 API（`/api/v1`），没有任何内置课程或假登录：平台没有账号体系，
`/auth` 是一个直通工作区的入口页；课程库为空时显示空态并引导去生成或导入。

## 开发

```bash
cd TeachingFlow/classroom/webapp
npm install
npm run dev
```

打开 <http://127.0.0.1:5173/>。

**dev 代理**（见 `vite.config.ts`）：`/api` 转到 `http://127.0.0.1:8000`，
`/ws` 转到 `ws://127.0.0.1:8000`（WebSocket）。后端用 `python app.py` 或
`./start-app.ps1` 起，默认 `HOST=127.0.0.1`、`PORT=8000`。

为什么代理而不是直连绝对地址：浏览器里前端和接口同源，跨域、Cookie、WebSocket 都不用额外配置；
代价是后端换端口时 `vite.config.ts` 要跟着改。

## 构建与类型检查

```bash
npx vue-tsc -b     # 只做类型检查
npm run build      # = vue-tsc -b && vite build
```

产物写在 `dist/`。

## 为什么 dist/ 入库

- 交付形态是「一条命令起服务」：`python app.py` 直接拿到可用界面，不需要目标机器装 Node。
- 后端在 `dist/index.html` 缺失时返回的是「前端还没构建」的说明页
  （`classroom/app/routes/static.py`），入库让这个状态只出现在真的忘了构建时。
- 代价要说清：改了 `src/` 就必须重建并一起提交 `dist/`，否则线上跑的还是旧包。
  评审时以 `dist/index.html` 引用的哈希文件名为准。

## 为什么资源必须走 /assets/*

后端只服务这几处（`TeachingFlow/classroom/app/routes/static.py`）：

| 请求 | 返回 |
| --- | --- |
| `GET /` | `dist/index.html` |
| `GET /assets/<相对路径>` | `dist/assets/<相对路径>`（做了目录穿越收敛） |
| `GET /assets/audio/<sha1>.mp3` | TTS 缓存（同一前缀下更具体的路由，先声明先匹配） |
| 其它路径 | SPA 兜底，同样返回 `dist/index.html` |

课件里的图片**不在** `/assets/` 下：page 帧的 `media_base` 指向
`/api/v1/units/{unit_id}/media/`（`classroom/app/routes/units.py`），重绘课件时别假设
媒体和前端资源同源同前缀。

结论：`public/` 里放根路径文件没有意义 —— 站点根目录下的单个文件（比如 favicon）
后端取不到，只会落到 SPA 兜底返回 HTML。所以图片、视频、字体一律 `import` 进 Vite 资源，
由构建指纹化后自然落进 `/assets/`：

```ts
import posterUrl from '@/assets/poster.png'                       // 图片
const videoUrl = new URL('../assets/motion-baked-desktop.mp4', import.meta.url).href
```

同理，源码里不允许出现指向旧仓库根目录的绝对资源地址（旧构建媒体目录、旧 prototype
资源目录那一套）；这类地址在这个后端下必然是死链。`public/` 目前为空，也不需要它。

## 深链与 SPA 兜底

路由用 history 模式，刷新 `/app/courses/:id`、`/app/units/:unitId/outline`、
`/app/sessions/:id/summary`、`/learn/:unitId` 都靠后端 `GET /{path:path}` 返回
`index.html` 才打得开。`src/router/index.ts` 里另外留了一组「旧地址 → 新路由」的重定向。

以 `api` / `ws` / `dev` / `assets` 开头的路径**不会**进兜底：写错的接口地址要返回 404，
一旦返回 HTML，排查的人会以为「接口存在但返回了网页」。

## 后端没起来时

`fetch` 失败统一抛 `ApiError(status=0)`（`src/api/client.ts`）。课程库页面显示错误态与
重试按钮，课程详情 / 小节页说明失败原因并提示先 `python app.py`；不拿任何本地内容顶上，
页面看到的课程一定来自课程库。

## 目录

```
src/api/            HTTP 契约类型、fetch 封装、视图模型翻译
src/components/     应用外壳、档案菜单、导入课件弹窗、Toast
src/composables/    落地页动效（滚轮驱动逐帧视频）
src/features/deck/  课件重绘（SlideView）
src/features/classroom/  课堂会话、WebSocket 传输、控制条、字幕
src/styles/         prototype/*、workspace.css、style.css（与旧仓库同源）
src/views/          6 个复刻页面 + Outline / Summary / LearnRedirect
```

样式与设计 token 与旧仓库逐字同源，不要在复刻页面里另起一套。
