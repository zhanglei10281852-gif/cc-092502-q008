# 考古研究协作基础服务

这是一个供考古项目扩展业务模块的纯后端基础服务，提供研究项目登记、成员与角色、会话认证、审计事件、幂等请求和可恢复后台任务。服务使用 FastAPI 与 SQLite，不依赖另行部署的数据库、缓存或队列。

## 环境与安装

运行环境为 Python 3.11。安装开发依赖：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

## 初始化与启动

```bash
python -m app.cli init-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

基础接口包括 `/api/system/health`、`/api/projects`、`/api/users`、`/api/sessions`、`/api/audit` 和 `/api/jobs`。首次启动后可用命令行创建管理员，也可以通过测试夹具构造隔离数据库。

## 测试

```bash
python -m pytest
```

测试覆盖数据库初始化、项目成员权限、会话撤销、审计脱敏、幂等写入和后台任务领取与完成。

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
```

该命令在进程内检查根路径、健康接口、数据库外键和 WAL 配置。

## 扩展约定

新研究模块应通过独立路由、服务和仓储接入，跨表写入放在即时事务中。外部标识、幂等键和审计载荷应保存原始值及规范化值；后台任务使用 SQLite 租约，不允许依赖外部队列。用户口令和会话令牌只保存摘要，审计事件会过滤密码、令牌等敏感字段。

## 陶片与器物分析模块

模块位于 `app/pottery/`，挂在 `/api/projects/{project_id}/pottery` 下，支持把来自不同灰坑的口沿、腹片、底部等碎片做拼合研究。

- **陶片记录**：`POST/GET/PUT /sherds` 记录器形部位（rim/body/base/handle/other）、胎色、厚度区间、纹饰编码、尺寸（口径/长/宽）、出土上下文（灰坑、层位）、类型编码与口沿保存百分比。编号保存原始值与规范化值，项目内唯一；已确认到器物的陶片禁止修改。
- **兼容规则**：`POST /rule-sets` 创建有版本的规则配置（部位兼容表、胎色分组、厚度容差、纹饰最小共有数、口径容差、上下文禁配、六维评分权重与阈值）。新版本生效后旧版本退役；候选生成按逐项评分确定性贪心成团——传递相似但直接不兼容的陶片不会同组，候选详情中的 `pair_scores` 解释每一对为何相似。
- **复核**：候选仅供复核。`confirm`/`reject`（候选）、`split`（拆组）、`merge`（合组）都必须填写依据并写入复核事件；确认关系强制成员唯一（数据库唯一索引 + 事务内校验）、上下文禁配与规则版本一致性。跨表写入在即时事务中完成，失败整体回滚。
- **统计**：`POST /stats/run` 按研究方案（plan_code + 上下文/部位范围）计算最小器物数、口沿当量（已确认器物按 1.0 封顶）与类型频次，每个统计值都带贡献者列表（可回溯到陶片或已确认器物）。输入不变时不产生新版本（幂等）；规则或分组变化后产生新结果版本，`GET /stats/diff` 展示两个版本间的数值与贡献者差异。
- **列表接口**：均支持 `cursor` 游标分页与稳定排序（排序键含唯一 id），`limit` 上限 200。
- **离线导入**：`POST /import` 接收结构化 JSON（规则 + 陶片 + 方案），单事务导入后直接触发候选生成与统计；任何冲突整体回滚。

角色分工：陶片/导入为 owner、researcher、recorder；规则与统计为 owner、researcher；复核操作为 owner、researcher、reviewer；读取为全部项目成员。
