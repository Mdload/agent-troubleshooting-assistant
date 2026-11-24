# Croc业务局端

本模块的日常知识入口。章节保留各自的模式、日期和适用条件；原始资料及出处单独存档。

## 局端

适用模式：normal_ha；设备组：local；证据日期：2026-09-28。

<!-- record:KB-005:historical_finding -->
局端正常HA中，VIP归属与本机Croc服务启停相配。局端示例Keepalived使用eth2单播，eth3承载业务侧；notify启停本机Croc。
健康判据涉及VIP归属、服务、进程、tun16、9965监听和路由。远端会话短暂消失不直接证明局端故障。
局端备机无 VIP 时通常不运行 Croc，tun16 DOWN 可符合角色。Croc 服务由本机角色回调停止；接管后才按本机条件启动。linkdown标记或残留路由不能单独证明有效转发或业务中断。
accept-watchdog只在完整条件满足时考虑本机恢复；不得将它执行成只读查询。
<!-- /record:KB-005:historical_finding -->

## 局端触发切换的规则

适用模式：normal_ha；设备组：local；证据日期：2026-09-28。

<!-- record:KB-013:historical_finding -->
示例主机优先级110、备机100；主机失败权重-20，降为90时健康备机100可能接管。fall 3、rise 2与preempt_delay 30属于不同配置条件。
降权不等于退出选举或必然进入FAULT，双机降权也不能推出没有候选、VIP必消失或业务必通。
eth2是示例VRRP接口；eth3的track_interface单独跟踪。跟踪项是否导致FAULT或降权取决于实际权重配置，不能一律等同对端收不到通告。
局端Croc服务与VIP在正常HA里的关系由本机角色决定。notify角色脚本只启停自身节点的Croc；MASTER启动本机，BACKUP/FAULT/STOP停止本机，不远程启停对端。
健康不是所有接管路径的必要条件：只降权且仍有候选资格的节点在对端退出等条件下仍可能持有VIP，实际角色、资格与优先级须另查。
暂时缺少远端会话不证明局端故障。VIP变化不等于端到端业务恢复。
<!-- /record:KB-013:historical_finding -->

## 局端健康判据和故障计数

适用模式：normal_ha；设备组：local；证据日期：2026-09-28。

<!-- record:KB-014:historical_finding -->
示例先检查eth2与eth3的LOWER_UP；owner在宽限期外核对服务active、进程存在、tun16 UP、9965监听、外层接入与内层业务路由。
MAX_OWNER_FAILURES=3是脚本失败计数，Keepalived fall 3是连续失败判据，rise 2是恢复计数，不能相乘成9次失败，也不是修复三次。
没有VIP的备机应停止Croc服务、进程和监听，不要求tun16 UP；备机残留linkdown路由不能单独确定状态。
健康检查失败仅表示该角色的某项检查未通过；owner和备机判据不同，不能推出接管后所有业务必断。具体路由失败需结合该业务目标和路径。
example-ha-health.sh会写目录、锁、计数及故障文件，不能执行为只读查询。
<!-- /record:KB-014:historical_finding -->

## 局端watchdog恢复与切换的区别

适用模式：normal_ha；设备组：local；证据日期：2026-09-28。

<!-- record:KB-015:historical_finding -->
accept-watchdog针对局端Croc接收队列的本机恢复，不等于重启服务器，也不等于直接切换主备。
示例要求本机持VIP、服务active、故障标记允许恢复；Recv-Q>0且两个远端内层地址均不可达才进入确认。
300秒冷却和15秒后二次确认是示例配置：再次检查VIP、服务、队列及不可达条件，才考虑本机停启Croc。
任意远端可达就跳过；队列非零或远端暂不可达单独均不足。恢复与Keepalived选举降权须分别解释，不承诺严格耗时。
<!-- /record:KB-015:historical_finding -->

## 局端备机停止与异常的区分

适用模式：normal_ha；设备组：local；证据日期：2026-09-28。

<!-- record:KB-016:historical_finding -->
不能只凭Croc inactive或tun16 DOWN判故障。无VIP备机与持VIP owner采用不同资源判据；必须有本轮服务、接口和VIP观察。
服务active而无VIP只报告不一致线索，模式、转换阶段和实际健康规则仍须核对。VIP或进程存在不等于业务通过。
<!-- /record:KB-016:historical_finding -->

## 配置阈值与实测恢复时间

适用模式：normal_ha；设备组：local；证据日期：2026-09-28。

<!-- record:KB-018:historical_finding -->
interval=2、fall=3不能直接推出严格6秒恢复：检查、超时、选举、角色回调与服务就绪是不同阶段。
MAX_OWNER_FAILURES与fall不相乘。preempt_delay=30、watchdog 300秒冷却及15秒确认都是条件，不是实测业务恢复时限。
<!-- /record:KB-018:historical_finding -->

## 远端切换触发局端不必要的级联切换

适用模式：normal_ha；设备组：local、remote；证据日期：无现场采集日期。

历史案例（anonymous_example），不代表当前设备状态。
匿名示例资料；不是历史修复成果或当前现场状态。

<!-- record:HA-003:historical_finding -->
示例将远端无会话错误归为局端自身故障。
<!-- /record:HA-003:historical_finding -->

<!-- record:HA-003:historical_outcome -->
应区分接收端故障与会话缺失；未验证真实级联或修复。
<!-- /record:HA-003:historical_outcome -->

## 备用节点Croc主动停止却显示failed

适用模式：normal_ha；设备组：local；证据日期：无现场采集日期。

历史案例（anonymous_example），不代表当前设备状态。
匿名示例资料；不是历史修复成果或当前现场状态。

<!-- record:HA-008:historical_finding -->
退出结果与当前资源归属需区分。
<!-- /record:HA-008:historical_finding -->

<!-- record:HA-008:historical_outcome -->
应核对停止原因，不直接启动备机或用reset-failed掩盖失败。
<!-- /record:HA-008:historical_outcome -->
