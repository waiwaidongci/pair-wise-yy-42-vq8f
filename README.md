# 山火事件指挥与离线人员调度

维护火线、风向、资源和任务区，合并离线现场记录并防止人员重复分配。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限、关闭不变量，以及火场看守与复燃判定。
- `src/repository.py`：SQLite建表、事务、版本控制、审计链和热点归档。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由、火场看守页面入口和统一错误响应。
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
- `GET /api/audit`
- `POST /api/items/{id}/hotspots`：登记热点（现场单号`ticket_no`、火线编号`line_id`、探测时刻`detected_at`、地表温度`surface_temperature`、烟点状态`smoke_state`：smoke/unknown/clear），同号重放返回首条并带`replay:true`
- `POST /api/items/{id}/hotspots/{ticket_no}/cooling`：更晚且降温的观测，必须由另一名巡线员执行，原登记记录保留
- `POST /api/items/{id}/hotspots/{ticket_no}/review`：由非观测人的另一名巡线员复核
- `POST /api/items/{id}/hotspots/{ticket_no}/correct`：更正登记温度/烟点/火线，最高温与待复测点按新值重算（旧降温复核作废，须重新复测）
- `GET /api/items/{id}/hotspots?line_id=`：热点归档列表
- `GET /api/items/{id}/lines`：每条火线的最高温、待复测点及复燃阻断项

允许角色：field_commander, incident_commander, logistics, viewer。火线长度、风向变化和离线记录数量影响风险等级；同一资源不能同时出现在多个活动任务中。

## 火场看守与复燃判定

判定规则在`src/rules.py`、热点归档在`src/repository.py`、页面入口与JSON路由在`src/http_api.py`，三处分离。任一火线还有超过80℃的热点、烟点未复测（最新状态非clear），或降温结果未由他人复核时，`controlled`/`closed`流转被阻断（409），火场停在原状态。降温结果在复核前不计入火线最高温；复核通过后才以降温观测值参与判定。热点登记/降温/复核/更正全部写入审计链。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
