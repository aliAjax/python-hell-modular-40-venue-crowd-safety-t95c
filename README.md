# 大型场馆人群安全与现场指挥

只使用Python标准库和SQLite的模块化服务，默认端口`8340`。支持场馆区域、入场口、容量、通道、安保岗位、医疗点、事件、限流、开放通道、疏散和医疗任务、人员到位、区域恢复、延迟重复事件和容量冲突。

区域疏导单（`diversion`）解决"区域快满时对讲机改道、到场才发现接收区域没余量"的问题：指挥员在同一场馆选两个不同区域创建疏导单，提交转入人数时即在同一事务内预占接收区域余量，其他疏导单不能重复使用；入口按实际人数放行，计入接收区域在场人数并释放对应占用，超过预占或容量一律拒绝；单子取消或过期后占用归还，原区域人数始终不变。

## 模块结构

- `app.py`：参数解析、依赖组装和服务生命周期。
- `src/domain.py`：角色、领域异常和数据对象。
- `src/rules.py`：容量计算、事件优先级、状态机和团队冲突约束。
- `src/repository.py`：SQLite持久化、乐观锁、幂等和审计查询。
- `src/service.py`：用例编排、权限校验和版本控制。
- `src/http_api.py`：JSON接口和统一错误响应。
- `src/audit.py`：操作审计。
- `static/index.html`：最小演示页面。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8340
```

## 核心对象

`venue`为场馆，`zone`为区域，`gate`为入场口，`post`为安保岗位，`medical_point`为医疗点，`incident`为事件，`task`为现场任务，`diversion`为区域疏导单。

## 区域疏导单

余量口径：`剩余 = 容量 - 在场人数 - 有效疏导单未用预占`，状态机为`reserved → partially_released → released`，或任意活动状态进入`cancelled`/`expired`。

- 创建：`POST /api/diversions`，字段`venue_id`、`source_zone_id`、`destination_zone_id`（须同场馆且互不相同）、`reserve_count`（转入人数）、`expires_at`（ISO时间，过期自动归还）、`reason`。指挥员/主管/管理员可创建，在单个`BEGIN IMMEDIATE`事务内校验余量并占名额，并发两张单不会超额。
- 放行：`POST /api/entities/<id>/actions`，`{"action":"release","data":{"gate_id","actual_count","admitted_at"}}`。入口操作员可用；校验入口开放且服务接收区域、`已放行+实际人数 ≤ 预占人数`、接收区域不超容量；成功后接收区域`current_occupancy`增加、预占相应释放，原区域人数不变。
- 取消：`action:"cancel"`，需`reason`，未用预占立即归还；部分放行后取消只归还剩余预占。
- 过期：到`expires_at`后，在下次查询/创建/放行时由惰性扫描置为`expired`并归还占用；过期单拒绝放行。
- 剩余名额：`GET /api/entities/<zone_id>/quota`返回`capacity`、`current_occupancy`、`reserved`、`remaining`及占用中的疏导单ID。

## 接口

- `GET /health`
- `GET /api/<kind>`，可用`?status=`过滤
- `GET /api/entities/<id>`
- `POST /api/<kind>`
- `POST /api/entities/<id>/actions`
- `GET /api/audit`

身份通过`X-User-Id`和`X-Role`请求头传入。可选`Idempotency-Key`防止重复创建。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

容量和调度规则为可运行的简化模型，不接入闸机、视频分析、室内定位、消防联动或真实应急指挥系统。
