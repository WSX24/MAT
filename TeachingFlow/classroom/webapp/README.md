# webapp · 课堂前端（Vite + React + TS）

契约唯一事实源：`TeachingFlow/classroom/docs/正式版设计.md` §4（HTTP/WS）、§6（页面清单）。
本目录只依赖那两份契约，不读后端源码。

## 开发

```bash
cd TeachingFlow/classroom/webapp
npm install
npm run dev          # http://127.0.0.1:5173，/api 与 /ws 代理到 127.0.0.1:8000
```

后端没起时用内置假数据独立开发：

```bash
VITE_MOCK=1 npm run dev
# 场景（首次读到的 ?mock= 会写进 sessionStorage，页面跳转后仍生效）：
#   ?mock=default   完整数据
#   ?mock=empty     课程库/大纲为空，扫描后才出现课程
#   ?mock=error     所有接口 500（验证错误态与「重试」）
#   ?mock=slow      每个请求 +1.1s（验证骨架屏）
#   ?mock=unprepared 单元未备课、大纲为空（验证「备课并生成大纲」）
```

## 类型与构建

```bash
npm run typecheck    # tsc --noEmit
npm run build        # tsc --noEmit && vite build → dist/
```

`dist/` 入库（由调度方提交），供 FastAPI 以 `GET /` 与 `/assets/*` 提供。
深链（如直接打开 `/courses/xxx`）需要后端补一个 SPA 回退：非 `/api`、非 `/ws`、非静态资源的 404 返回 `dist/index.html`。

## 设计约束

- 令牌：`src/styles/tokens.css`（色彩/字阶/间距/圆角/阴影，深浅两套；基准字号 17px 面向投屏）。
- 基础组件自带，不引入 UI 库：Button / Card / Progress(Ring) / Badge / Modal / Toast / Empty / Skeleton / Field。
- 课件重绘：`src/features/deck/SlideView.tsx`，1280×720 逻辑画布 + `transform: scale()`，字号 px = pt × 4/3。
- 无障碍：焦点可见、按钮 aria-label、课堂内空格暂停/继续、←→ 翻页。
- 断点：1279px（投屏以下的窄屏，收起单元进度列、侧栏变窄）、768px（手机，课堂侧栏变底部抽屉）。
