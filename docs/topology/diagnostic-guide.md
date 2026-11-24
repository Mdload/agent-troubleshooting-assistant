# 匿名示例资料：诊断解释
此文是示例规则的归纳，不是新现场观测。

## 局端备机停止与异常的区分
不能只凭Croc inactive或tun16 DOWN判故障。无VIP备机与持VIP owner采用不同资源判据；必须有本轮服务、接口和VIP观察。
服务active而无VIP只报告不一致线索，模式、转换阶段和实际健康规则仍须核对。VIP或进程存在不等于业务通过。

## 排障路径与工具覆盖的边界
远端ping可达但业务不通时，从下挂终端核对指定源地址和回程，并分别看管理可达、外层接入、Croc隧道和内层业务。Router只转发，Croc终止于局端与远端。
示例工具登记四业务角色，services读指定单元，interfaces读标志/地址及9965连接，routes 读主路由表及策略规则，policy_routes 可读取策略表。Router 实时状态和端到端业务收发仍需独立证据。
systemctl active只支持服务状态；active/exited且MainPID=0可为已完成one-shot。接口UP不是完整隧道验收，ESTAB也不是业务流量证明。
根据tool_catalog和本轮成功OBS判断覆盖，失败结果不得当成功引用；管理链可达不证明业务链通。

## 配置阈值与实测恢复时间
interval=2、fall=3不能直接推出严格6秒恢复：检查、超时、选举、角色回调与服务就绪是不同阶段。
MAX_OWNER_FAILURES与fall不相乘。preempt_delay=30、watchdog 300秒冷却及15秒确认都是条件，不是实测业务恢复时限。
