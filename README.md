# 山火事件指挥与离线人员调度

维护火线、风向、资源和任务区，合并离线现场记录并防止人员重复分配。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8319
```

默认端口为`8319`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/items/{id}/hotspots`，返回热点列表及每条火线的最高温、待复测点汇总
- `POST /api/items/{id}/hotspots`，看守热点登记（现场单号`ticket_no`、火线`fireline`、探测时刻`detected_at`、地表温度`surface_temp`、烟点状态`smoke_status`）；同号重放返回首条（HTTP 200，`replayed=true`），不重复计数
- `POST /api/items/{id}/hotspots/{ticket_no}/cooling`，更晚的降温观测（须晚于探测时刻，只允许一次）
- `POST /api/items/{id}/hotspots/{ticket_no}/review`，由另一名巡线员（不同于登记人与观测人）复核降温；原始登记与观测均保留
- `POST /api/items/{id}/hotspots/{ticket_no}/smoke-retest`，烟点复测
- `POST /api/items/{id}/hotspots/{ticket_no}/correct`，热点更正（最高温与待复测点按新值重算，旧值进审计链）
- `GET /api/audit`

允许角色：field_commander, incident_commander, logistics, viewer。火线长度、风向变化和离线记录数量影响风险等级；同一资源不能同时出现在多个活动任务中。

火场转入看守（`contained`）后才能登记热点。宣布控制（`controlled`）前满足任一条件即拦截、火场停在原状态：任一火线仍有超过80℃的热点（降温以复核值为准）、仍有烟点未复测、降温结果未经另一名巡线员复核。判定逻辑在`src/rules.py`，热点归档在`src/repository.py`，HTTP与页面入口在`src/http_api.py`和`static/index.html`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
