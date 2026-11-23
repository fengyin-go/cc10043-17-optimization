# API数据接入与消息流处理平台

面向第三方API和消息队列的数据接入、流式处理、水位管理和断点续采的采集治理平台

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/routers/resume_flow.py 检查点确认流转/续采受理专用接口
│   ├── app/services/checkpoint_flow.py 续采凭据冻结/受理/交接/公布/引用留痕
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据（含续采留痕台账）
├── scripts/
│   ├── verify_checkpoint_flow.py 服务层场景验证（两个批准时点+隔离+反查）
│   └── verify_http_flow.py       真实 HTTP 端到端验证
├── .gitignore
└── docker-compose.yml
```

## 检查点确认流转（断点续采治理）

围绕「切换前后实际续采范围一致」建立的硬约束，领域逻辑全部集中在
`app/services/checkpoint_flow.py`，路由层不做业务判断：

- **精确归属**：检查点只按「关联接入任务」等值匹配，同名前缀任务（如
  `订单接入任务A` 与 `订单接入任务A-备份链路`）候选互不串用。
- **候选门槛**：只有「已确认」检查点能进可用候选；待确认草稿、已过期归档点
  一律排除，确认时分配任务内单调递增的版本号。
- **凭据冻结**：续采受理瞬间冻结检查点版本与起始 Offset，开立受理单
  （`resume_acceptance` 台账）。受理后再批准的新断点不回改本轮；与在途区间
  冲突时只提示「候任下一轮」，已交给本轮的区间（如 101-120）始终有人负责。
- **逐段留痕**：每次读取生成一条读取段（`resume_read_segment` 台账），记录
  分区、实读区间、条数、采用检查点版本、凭据当前状态与归属受理单。
- **换点交接**：旧受理单保留实读责任，未实读部分显式给出兜底区间并自动开立
  采用新检查点的后继受理单（`checkpoint_handover` 台账），禁止跨任务换点。
- **公布结算**：公布本轮实际覆盖区间、采用检查点与交接责任，并写入流审计。
- **归档可查**：检查点废弃只改状态不删数据，可通过引用反查接口追到历史受理单、
  读取段与交接记录。

### 专用接口（先于通用资源路由注册）

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/record_checkpoint/eligible?task=` | 精确任务的已确认候选（草稿/近名任务不返回） |
| GET | `/api/record_checkpoint/{id}/references` | 检查点引用反查（含已归档） |
| GET | `/api/resume_policy/tasks/acceptances?task=` | 任务受理单及每段实读/交接明细 |
| POST | `/api/resume_policy/accept` | 受理续采，冻结检查点版本与起点 |
| POST | `/api/resume_policy/acceptances/{id}/read-segment` | 按冻结凭据读取下一段 |
| POST | `/api/resume_policy/acceptances/{id}/handover` | 换点责任交接并开立后继单 |
| POST | `/api/resume_policy/acceptances/{id}/publish` | 公布本轮实际覆盖区间 |
| GET | `/api/resume_policy/acceptances/{id}` | 受理单详情 |

前端在导航「检查点续采流转」（`/resume-flow`，`ResumeFlowView.vue`）提供候选、
受理、逐段区间、交接与公布的可视化工作台；通用「记录检查点」列表额外展示
凭据状态、冲突提示与引用受理单数。

### 验证

```bash
python3 -I scripts/verify_checkpoint_flow.py   # 服务层 52 项场景断言
python3 -I scripts/verify_http_flow.py         # 真实服务 27 项 HTTP 断言（需先起后端）
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8017/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5197/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8017`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| API连接器 | `api_connector` | API连接器 | 连接器名称、API端点、认证方式 |
| 消息流源 | `stream_source` | 消息流源 | 源名称、中间件类型、Topic/Queue名 |
| 偏移量管理 | `offset_manager` | 偏移量记录 | 消费组、Topic/Queue、分区/分片 |
| API调用调度 | `api_scheduler` | 调用计划 | 计划名称、关联API、Cron表达式 |
| 记录检查点 | `record_checkpoint` | 检查点 | 关联接入任务、检查点类型、检查点值 |
| 消息解析器 | `message_parser` | 解析模板 | 模板名称、消息格式、字段映射 |
| 去重中心 | `dedup_center` | 去重键 | 去重键名、关联数据源、去重窗口秒数 |
| 水位追踪 | `watermark_tracker` | 水位记录 | 数据源、水位字段、当前水位 |
| 频控保护 | `rate_guard` | 频控规则 | 规则名称、限流维度、每秒上限 |
| 失败隔离区 | `failure_isolation` | 隔离记录 | 失败消息ID、失败原因、原始Topic |
| 流数据处理审计 | `stream_audit` | 审计明细 | 处理时间、数据源、消息总数 |
| 批量消息回放 | `batch_replay` | 回放任务 | 回放范围、开始时间、结束时间 |
| 字段转换器 | `field_transform` | 转换规则 | 规则名称、源字段、目标字段 |
| API调用日志 | `api_logger` | 调用日志 | 调用时间、API名称、请求参数摘要 |
| 数据落地配置 | `data_sink` | 落地配置 | 配置名称、目标存储类型、写入模式 |
| Schema注册中心 | `schema_registry` | Schema版本 | Schema名称、版本号、字段定义 |
| 流式数据导出 | `stream_export` | 导出任务 | 任务名称、数据源、导出格式 |
| 续采策略 | `resume_policy` | 续采规则 | 规则名称、续采起点类型、续采步长 |
| 消息载荷存储 | `payload_store` | 原始载荷 | 采集批次、消息ID、原始载荷 |
| 吞吐量告警 | `throughput_alarm` | 告警规则 | 规则名称、监控数据源、吞吐量下限 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
