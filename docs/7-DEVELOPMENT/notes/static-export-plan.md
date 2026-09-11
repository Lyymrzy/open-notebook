# 前端静态导出迁移计划(方案①:FastAPI 同源单进程)

- **状态**:已规划(Step 1 盘点完成 2026-09-11;Step 2 未实施)
- **相关**:[ADR-009](../decisions/ADR-009-notebook-ai-core-fork.md)(笔记本+AI 精简 fork)、分支 `wip/async-transformation-execute`
- **目标**:去掉运行时 Node(前端不再跑 Next 服务端),体积再降 ~100–130MB,进程 3→2、端口 2→1

## 决策

1. **动态路由改查询参数**:`/notebooks?id=<id>`、`/sources?id=<id>`(纯静态、最稳,无宿主回退依赖)。
2. **托管方式**:`output: 'export'` 产出 `out/`,由 **FastAPI `StaticFiles` 同源托管**;`/api/*` 仍由 FastAPI 处理。前端相对 `/api/...` 调用因此**零改动**。

## 为什么可行(关键事实)

- 所有页面与布局都是 `'use client'`,无服务端取数、无 `next/headers`、`cookies()`、Server Actions、ISR。
- 客户端 API 一律走相对路径(`apiClient`、`lib/api/search.ts`、`lib/api/source-chat.ts`);同源托管后直达 FastAPI。
- 无 `next/image` 用法;`next/font/google` 为**构建期**下载自托管,导出兼容。

## 影响面盘点(A–E)

### A. 服务端运行时件(导出后不存在)

| 文件 | 作用 | 处理 |
|---|---|---|
| `src/proxy.ts` | Next 中间件:`/` → `/notebooks` 重定向 | 删除;改由 FastAPI 路由重定向 |
| `src/app/config/route.ts`(+`route.test.ts`) | 运行时下发 API 地址 | 删除;`src/lib/config.ts` 去 `fetch('/config')`,同源默认 `apiUrl=''` |
| `src/app/api/search/ask/route.ts` | SSE 代理 → FastAPI `/api/search/ask` | 删除(客户端相对路径同源直连,无需改客户端) |
| `src/app/api/sources/[sourceId]/chat/sessions/[sessionId]/messages/route.ts` | SSE 代理 | 删除(同上) |
| `src/app/api/_sse-proxy.ts` | 代理 helper(读 `INTERNAL_API_URL`) | 随路由删除 |

### B. 服务端页面

| 文件 | 现状 | 处理 |
|---|---|---|
| `src/app/page.tsx:4` | `redirect('/notebooks')` | 删除;FastAPI `/` → 重定向 `/notebooks` |
| `src/app/(dashboard)/page.tsx:4` | `redirect('/notebooks')` | 同上 |
| `src/app/dev/design/page.tsx` | 开发用视觉指南 | 确认生产门控;导出会静态暴露 → 删除或加环境门控 |

### C. 动态路由(改查询参数)

| 页面 | UI 链接点(需同步改) |
|---|---|
| `(dashboard)/notebooks/[id]/page.tsx`(`useParams`) | `NotebookCard.tsx:40`、`NotebookRow.tsx:41,57`、`RecentlyViewed.tsx:27`、`CommandPalette.tsx:225` |
| `(dashboard)/sources/[id]/page.tsx`(`useParams`) | `RecentlyViewed.tsx:30`、`sources/page.tsx:128,261`、`SourceDialog.tsx:31` |

改法:页面移到 `notebooks/view/page.tsx` / `sources/view/page.tsx`,用 `useSearchParams().get('id')` 取参;上述链接改 `/notebooks?id=…`、`/sources?id=…`。

### D. 构建 / 镜像 / 进程装配

| 位置 | 现状 | 改动 |
|---|---|---|
| `frontend/next.config.ts` | `output:'standalone'`、`rewrites()`、`redirects()`、`experimental.proxyClientMaxBodySize` | 改 `output:'export'` + `trailingSlash:true`;删 rewrites/proxyClientMaxBodySize;`/settings/api-keys` 重定向移到 FastAPI;可加 `images.unoptimized:true` |
| `supervisord.conf` `[program:frontend]` | `node server.js`(8502) | 删除该 program |
| `frontend/start-server.js` | 启 standalone server | 不再需要 |
| `Dockerfile`(runtime-base) | apt 装 Node.js 22;拷 `.next/standalone`/`.next/static`/`public`/`start-server.js`;`EXPOSE 8502 5055` | runtime **不再装 nodejs**;改拷 `out/`;`EXPOSE 5055` |
| `scripts/wait-for-api.sh` | 被 frontend program 使用 | 随 frontend program 移除/保留无碍 |

### E. 后端(FastAPI 托管静态)

- 末尾挂 `StaticFiles(directory="frontend/out", html=True)`(**必须在所有 `/api` 路由之后**);
- 显式路由:`GET /` → `RedirectResponse('/notebooks')`;`GET /settings/api-keys` → `RedirectResponse('/settings/models')`(兼容旧链接);
- 旧动态深链兼容(可选):`GET /notebooks/{id}` → 301 到 `/notebooks?id={id}`(收藏夹/旧链接不 404);
- `MaxBodySizeMiddleware`(100MB)不变;同源后不再需要 CORS(保持现状也无害)。

## Step 2 实施顺序(逐文件)

1. **前端路由**:`next.config.ts` 改 export/trailingSlash;删 `proxy.ts`、`config/route.ts`(+test)、`api/**`、两个根 `page.tsx`;`config.ts` 去 `/config` 探测;动态页改 `view/page.tsx` + 改 7 处链接。
2. **后端托管**:`api/main.py` 末尾挂 StaticFiles + `/` 与 `/settings/api-keys` 重定向(+可选旧深链 301)。
3. **进程/镜像**:`supervisord.conf` 删 frontend;`Dockerfile` runtime 去 nodejs、拷 `out/`、改 `EXPOSE`;清理 `start-server.js` 引用。
4. **测试**:删/改 `config/route.test.ts` 及引用被删文件的用例;`npm run build` 产出 `out/` 校验。
5. **验证**(见下)。

## 验证清单

- 深链刷新:`/`、`/notebooks`、`/notebooks?id=…`、`/sources?id=…`、`/transformations`、`/settings/models`。
- **SSE 流式**:Ask 搜索、Source Chat(同源直连;确认无缓冲)。
- 大文件上传(≤100MB)经 FastAPI。
- 旧链接:`/settings/api-keys` → `/settings/models`;可选 `/notebooks/<id>` 301。
- 无 Node 进程;`docker images` 体积对比(目标 ~1.5GB)。
- 回归:转换 execute-async、模型设置可用、TTS/STT 仍隐藏、`/api/podcasts*` 仍 404。

## 风险

- **SSE 缓冲**:同源直连后无 Next 中转,风险低;仍需实测流式逐块到达。
- **深链一致性**:`trailingSlash` 与目录式 `index.html` 需与 FastAPI `StaticFiles(html=True)` 配合;旧 `/notebooks/<id>` 会 404 → 用可选 301 兼容。
- **dev/design 暴露**:导出后成为普通静态页,必须门控或删除。
- **行为变化**:根路径由客户端/host 重定向替换,注意首屏闪烁与 SSR 之外的初始化顺序。
