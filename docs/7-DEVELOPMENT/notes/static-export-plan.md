# 前端静态导出迁移计划(方案①:FastAPI 同源单进程)

- **状态**:✅ 已实施(2026-09-26;Step 1 盘点 2026-09-11;镜像实测 2.07GB → **1.09GB**)
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
| `supervisord.conf` `[program:frontend]` | `node server.js`(8502) | ✅ 已删除该 program |
| `frontend/start-server.js` | 启 standalone server | ✅ 已删除;`package.json` 的 `start` 一并去掉 |
| `Dockerfile`(runtime-base) | apt 装 Node.js 22;拷 `.next/standalone`/`.next/static`/`public`/`start-server.js`;`EXPOSE 8502 5055` | runtime **不再装 nodejs**;改拷 `out/`;`EXPOSE 5055` |
| `scripts/wait-for-api.sh` | 被 frontend program 使用 | ✅ 已删除 |

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

## 实施结果(2026-09-26)

| 阶段 | 内容 | 提交 |
|---|---|---|
| A 前端 | `output:'export'`、删服务端路由/中间件、动态路由改查询参数 | `db6658d` |
| B 后端 | `StaticFiles` 同源托管、根路径重定向、鉴权仅限 `/api`、旧链接 301 | `1a8ad1b` |
| C 镜像 | runtime 去 Node、只拷 `out/`、`EXPOSE 5055`、supervisord 只剩 api+worker | `1a8ad1b` |
| 收尾 | 单端口 compose、删 `start-server.js`/`wait-for-api.sh`、旧深链 301 | `bc4658c` |
| 修复 | 深链重写改中间件(见下“踩坑”)| `c4983f7` |

实测(容器 `127.0.0.1:5055` 单端口):`/` 307 → `/notebooks/`;页面/RSC 载荷均 200;旧深链 301;
SSE 聊天端到端(回复已落库、含来源引用);播客路由仍 404;`/usr` 224MB、`frontend/out` 9.4MB、镜像 1.09GB。

### 踩坑:导出的 RSC 载荷文件名会被通配路由吞掉

`next build`(export)除了 `notebooks/index.html` 还会产出 `notebooks/index.txt`
（客户端路由预取用的 RSC 载荷）。最初的旧深链兼容写成 `@app.get("/notebooks/{notebook_id}")`
通配路由,于是 `/notebooks/index.txt` 被当成 id → 301 到 `/notebooks/view?id=index.txt`,
SPA 跟着跳过去显示“笔记本不存在”,再请求 `/api/notebooks/index.txt` → 500
(`No class found for table index.txt`)。

**做法**:改用中间件 + 精确正则 `^/(notebooks|sources)/([^/]+)$`,且只重写**含 `:` 的真实记录 id**
(`notebook:abc123`),其余一律留给静态挂载。新增类似兼容时不要用 `{param}` 通配路由。

## 遗留(未做/可选)

- 上游文档与示例(README、`docs/0-START-HERE`、`docs/5-CONFIGURATION`、`examples/*`、`scripts/release-test/*`)
  仍描述 8502(Next 服务端)拓扑,本次未改:与本 fork 的单端口形态不一致,按需同步。
- `/api/providers` 的 `modalities` 仍列出 `text_to_speech`/`speech_to_text`(provider 目录元数据,
  非可用模型类型;创建/默认/发现三处已拦截)。
- `frontend/src/app/dev/design` 静态导出后仍可访问(`/dev/design`);如需隐藏需加门控。

## 验证清单

- 深链刷新:`/`、`/notebooks`、`/notebooks?id=…`、`/sources?id=…`、`/transformations`、`/settings/models`。
- **SSE 流式**:Ask 搜索、Source Chat(同源直连;确认无缓冲)。
- 大文件上传(≤100MB)经 FastAPI。
- 旧链接:`/settings/api-keys` → `/settings/models`;可选 `/notebooks/<id>` 301。
- 无 Node 进程;`docker images` 体积对比(目标 ~1.5GB)。
- 回归:转换 execute-async、模型设置可用、TTS/STT 仍隐藏、`/api/podcasts*` 仍 404。

## 风险

- ~~**SSE 缓冲**~~:已实测逐块流式正常(同源直连无中转)。
- ~~**深链一致性**~~:已用 301 兼容旧 `/notebooks/<id>`;注意别用通配路由(见上“踩坑”)。
- **dev/design 暴露**:导出后 `/dev/design` 成为普通静态页,尚未门控。
- **行为变化**:根路径重定向与静态托管由服务端接管,首屏不再经过 Next 服务端。
