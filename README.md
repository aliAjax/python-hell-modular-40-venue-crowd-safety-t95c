# 大型场馆人群安全与现场指挥

只使用Python标准库和SQLite的模块化服务，默认端口`8340`。支持场馆区域、入场口、容量、通道、安保岗位、医疗点、事件、限流、开放通道、疏散和医疗任务、人员到位、区域恢复、延迟重复事件和容量冲突，并提供区域疏导单（容量预占、入口放行、取消与过期归还）。

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

指挥员（`supervisor`/`coordinator`/`admin`）在同一场馆选两个不同区域创建疏导单，提交`requested_count`时即在事务内占住接收区域余量，其他疏导单与普通入场都不能重复使用该余量：

- `POST /api/diversions`：`venue_id`、`source_zone_id`、`target_zone_id`、`requested_count`必填，两个区域必须不同且属于同一场馆；可选`expires_at`（ISO-8601）。状态从`reserved`开始，原区域人数不变。
- `admit`：入口（`operator`等）登记`gate_id`、`count`、`admitted_at`，实际人数计入接收区域`current_occupancy`并释放等量预占；超过该单剩余名额、入口未开放或不服务接收区域时拒绝（409/400）。全部放完后单子变为`completed`。
- `cancel`：需`reason`，未用余量归还接收区域，已放行人数保留，单子变为`cancelled`。
- 过期：到达`expires_at`后预占不再计入余量；对此单放行会被拒绝（`ExpiredDiversion`）并自动落为`expired`，也可由`POST /api/diversions/expire-due`批量扫描归还。
- 区域视图新增`held_count`（被有效疏导单预占）、`available_count`（容量−在场−预占）；疏导单视图含`remaining_count`、`is_expired`。
- 所有容量检查与占额/释放都在单个`BEGIN IMMEDIATE`事务内完成，并发下单不会超卖；支持`Idempotency-Key`。

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
