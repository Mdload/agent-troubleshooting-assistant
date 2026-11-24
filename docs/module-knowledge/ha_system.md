# 双机热备系统

本模块的日常知识入口。章节保留各自的模式、日期和适用条件；原始资料及出处单独存档。

## 三层分工

适用模式：按正文说明；设备组：按正文说明；证据日期：2026-09-28。

<!-- record:KB-001:historical_finding -->
入门解释：示例文件从现场电脑出发，由远端封装并经过中间转发设备，到局端解封后交给目标终端。候选检查通过只支持其中一段前提，端到端业务仍需核对实际收发。

示例业务路径为远端下挂业务 → 远端Croc SMR → PPP或有线路径及中间交换路由网络 → 局端Croc SAR → tun16内层网络 → 局端业务侧。响应反向。
这是三层独立热备的功能关系。示例管理跳板与接入网关只负责SSH管理，SSH能连跳板不证明业务通，也不证明完整物理接线。
<!-- /record:KB-001:historical_finding -->

## 外层与内层

适用模式：按正文说明；设备组：按正文说明；证据日期：2026-09-28。

<!-- record:KB-002:historical_finding -->
匿名例中Croc接收VIP为192.0.2.15，Router下一跳VIP为192.0.2.25；两者职责不同。外层TCP9965连接与内层tun16业务要分别看。
ppp4、ppp5、ppp6及eth1、eth2、eth3是示例候选接口名；ESTAB只描述连接，不证明业务吞吐、完整路径或算法效果。
示例远端业务VIP为198.51.100.29，局端业务VIP为192.0.2.37。地址全部虚构，不提供现场映射。
示例固定 IP 与 VIP 分开登记。局端 tun16 为 203.0.113.1，远端主备分别为 203.0.113.133 和 203.0.113.134。备机接管时可使用自身的内层地址。

Keepalived 心跳使用同侧主备之间的 VRRP 通告，不经 Croc 业务隧道。

配置含注释时不能直接当标准JSON解析。schType=91是示例配置值；没有调度实现及相同条件对照数据，不能推出算法91有效。
<!-- /record:KB-002:historical_finding -->

## 设备与程序

适用模式：按正文说明；设备组：按正文说明；证据日期：2026-09-28。

<!-- record:KB-008:historical_finding -->
| 示例设备 | 示例固定地址 | 主要程序与入口 | 本次资源状态 |
| --- | --- | --- | --- |
| 局端主机 | 192.0.2.13 | Croc_sar、Keepalived | 生成的历史夹具记录：服务处于 active |

四个业务角色为local-primary、local-backup、remote-primary、remote-backup；名称是登记标签，不证明当前主机是谁。
局端使用Croc SAR与Keepalived；远端使用Croc SMR、Keepalived及业务路由单元。备机拨号可常驻而Croc停止。
Router负责转发；示例管理跳板及接入网关只负责管理。此例没有设备实时状态。
<!-- /record:KB-008:historical_finding -->

## 关键程序路径

适用模式：按正文说明；设备组：按正文说明；证据日期：2026-09-28。

<!-- record:KB-009:historical_finding -->
以下名称用于示例职责索引。

- 测试节点示例 agent.py 配合控制器记录测试模式。
- 远端启动由 rc.local 调用拨号入口。Croc 由 systemd 和本机角色回调负责启动。
- 远端 remote-ha-candidate.sh 核对候选资格，remote-ha-health.sh 检查角色资源；manage_services.sh 调度示例常驻程序。

远端启动：rc.local 可调用示例拨号入口。当前演示架构中启动 Croc 的职责由 systemd 和 HA 角色回调承担。局端由 notify 启动或停止业务服务。
monitor collector会写采集结果，控制器会改变运行模式，健康及watchdog脚本可能写状态，均不能当作只读工具执行。
示例代码使用 example-croc-sar.service、example-croc-smr.service 和 example-ha-routes.service 对应这三类程序。
看到磁盘旧启动脚本不等于正在运行；数据库和前端业务关系没有采集。
<!-- /record:KB-009:historical_finding -->

## 角色与网络细节

适用模式：按正文说明；设备组：按正文说明；证据日期：2026-09-28。

<!-- record:KB-010:historical_finding -->
示例局端Keepalived通过eth2做心跳单播，eth3为业务侧；其他接口有地址也不证明是心跳口。
远端eth4可承担心跳，PPP可承担候选业务链路。角色、VIP、连接和路由是不同观察范围。
名称可重复，身份检查须结合登记ID与虚构固定示例地址；本候选不提供实际注册或管理连接。
<!-- /record:KB-010:historical_finding -->

## Router

适用模式：normal_ha；设备组：router；证据日期：2026-09-28。

<!-- record:KB-003:historical_finding -->
Router示例同步七个VRRP实例，使用优先级150与100；它负责路由转发及VIP同步，不终止Croc隧道。
模拟 TOR4 交换机故障时，物理 MAC 与 VRRP 虚拟 MAC 要分别核对。设备重启可能触发表项更新，须以复测确认。
只读观察转发开关与路由，不执行可能写状态的检查脚本。接口配置和完整接线属于不同证据。
<!-- /record:KB-003:historical_finding -->

## 远端

适用模式：normal_ha；设备组：remote；证据日期：2026-09-28。

<!-- record:KB-004:historical_finding -->
入门解释：远端承接现场电脑流量，活动节点启动本机 Croc 并维持业务入口。

示例基站断电时，可按地址、选路和可达性核对剩余有线候选。远端候选配置可包含PPP和有线接口；至少一条路径合格是示例条件，不等于Croc业务已通。
示例 remote-ha-candidate.sh 的 candidate检查管理/业务前提，候选路径至少一条合格接受。候选检查通过只证明端到端业务路径的部分前提。
remote-ha-routes.service 管理业务路由，Croc 承担传输。管理拨号与SSH保持常驻。

远端正常HA中，VIP归属与本机Croc服务启停相配。MASTER在本机启动Croc与业务路由，BACKUP/FAULT/STOP停止本机业务资源，拨号和管理可继续。
Croc ExecCondition依业务VIP决定是否允许启动；没有VIP时启动被跳过可能是保护，不能直接删门禁。
示例选举优先级为主节点 120、备节点 100。示例MAX_OWNER_FAILURES=3记录失败，不代表修复三次；配置hold=30秒、preempt_delay=15也不证明实测恢复时间。
<!-- /record:KB-004:historical_finding -->

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

## 排障路径与工具覆盖的边界

适用模式：按正文说明；设备组：按正文说明；证据日期：2026-09-28。

<!-- record:KB-017:historical_finding -->
远端ping可达但业务不通时，从下挂终端核对指定源地址和回程，并分别看管理可达、外层接入、Croc隧道和内层业务。Router只转发，Croc终止于局端与远端。
示例工具登记四业务角色，services读指定单元，interfaces读标志/地址及9965连接，routes 读主路由表及策略规则，policy_routes 可读取策略表。Router 实时状态和端到端业务收发仍需独立证据。
systemctl active只支持服务状态；active/exited且MainPID=0可为已完成one-shot。接口UP不是完整隧道验收，ESTAB也不是业务流量证明。
根据tool_catalog和本轮成功OBS判断覆盖，失败结果不得当成功引用；管理链可达不证明业务链通。
<!-- /record:KB-017:historical_finding -->

## 固定主机测试模式

适用模式：fixed_primary_test；设备组：按正文说明；证据日期：2026-09-28。

<!-- record:KB-006:historical_finding -->
示例控制器固定业务VIP，抑制选举和备机业务启动；退出时释放固定资源再恢复HA。
测试模式下tun地址与路由恢复不代表正常HA也完成同样验收。控制器状态、drop-in与角色需分别核对，不盲目重复进入模式。
<!-- /record:KB-006:historical_finding -->

## 对排障助手的要求

适用模式：按正文说明；设备组：按正文说明；证据日期：2026-09-28。

<!-- record:KB-007:historical_finding -->
三层独立热备中的 PPP 探测应核对指定源地址与策略路由。普通 ping 使用系统默认选路，可能走管理 PPP；绑定业务源地址时需结合对应策略表和回程判断。

先识别设备和模式，区分匿名示例、静态配置、历史记录、合成观测和真实设备证据。
直接读取systemctl属性、ip、ss等有界信息；example-ha-health.sh等健康脚本会写锁、目录、计数或故障标记，不能包装为只读工具。
端口、PID、VIP或一次ping都不足以证明完整业务恢复。解释结论时保留来源、条件和未知项。
<!-- /record:KB-007:historical_finding -->

## 已识别的冲突

适用模式：按正文说明；设备组：按正文说明；证据日期：2026-09-28。

<!-- record:KB-011:historical_finding -->
示例中旧PPP-only规则与后续含有线的候选集合属于不同版本，不能混用；三VLAN与六VLAN设计也需说明版本。
ppp5旧案例描述无载波无地址且未修复，不说明现在ppp5仍坏；新合成快照是否有地址仅按该快照回答。
各设备墙钟可能有时区与偏差，不能直接拼接日志时间认定全局先后；需统一参考、当时偏移和不确定度。
<!-- /record:KB-011:historical_finding -->

## 未确认与后续取证

适用模式：按正文说明；设备组：按正文说明；证据日期：2026-09-28。

<!-- record:KB-012:historical_finding -->
交换机完整接线、VLAN成员、终端回程及端到端业务质量没有真实证据；不能扫描或猜管理入口。
Croc算法91的调度实现、加载配置链、效果及重排参数未审阅；配置值和ESTAB不支持算法性能结论。
数据库应用关系、真实角色与接管是否成功都未验证；示例单元检查不证明独立人工评审或真实故障定位。
<!-- /record:KB-012:historical_finding -->

## 无线全断后，有线可达却未取得业务VIP

适用模式：normal_ha；设备组：remote；证据日期：无现场采集日期。

历史案例（anonymous_example），不代表当前设备状态。
匿名示例资料；不是历史修复成果或当前现场状态。

<!-- record:HA-001:historical_finding -->
示例候选只检查PPP，Croc配置中的有线接口不能替代候选资格。
<!-- /record:HA-001:historical_finding -->

<!-- record:HA-001:historical_outcome -->
需要核对候选配置与VIP门禁；未执行修复。
<!-- /record:HA-001:historical_outcome -->

## 健康检查只覆盖一张PPP，遗漏其他健康路径

适用模式：normal_ha；设备组：remote；证据日期：无现场采集日期。

历史案例（anonymous_example），不代表当前设备状态。
匿名示例资料；不是历史修复成果或当前现场状态。

<!-- record:HA-002:historical_finding -->
示例PPP-only条件遗漏其他候选；是否any-healthy须读取规则。
<!-- /record:HA-002:historical_finding -->

<!-- record:HA-002:historical_outcome -->
候选可达不等于Croc业务健康，示例不含真实变更结果。
<!-- /record:HA-002:historical_outcome -->

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

## BACKUP手动启动Croc后无法正常恢复选主

适用模式：normal_ha；设备组：remote；证据日期：无现场采集日期。

历史案例（anonymous_example），不代表当前设备状态。
匿名示例资料；不是历史修复成果或当前现场状态。

<!-- record:HA-004:historical_finding -->
资源归属与角色不一致是示例线索。
<!-- /record:HA-004:historical_finding -->

<!-- record:HA-004:historical_outcome -->
无VIP的ExecCondition启动被跳过可能是保护，不能删除门禁。
<!-- /record:HA-004:historical_outcome -->

## Croc重新拉起后tun16地址或业务路由缺失

适用模式：fixed_primary_test；设备组：local、remote；证据日期：无现场采集日期。

历史案例（anonymous_example），不代表当前设备状态。
匿名示例资料；不是历史修复成果或当前现场状态。

<!-- record:HA-005:historical_finding -->
示例只恢复进程而没有恢复内层资源。
<!-- /record:HA-005:historical_finding -->

<!-- record:HA-005:historical_outcome -->
固定主机测试模式的恢复不能推广到正常HA，未做真实SIGKILL。
<!-- /record:HA-005:historical_outcome -->

## 固定主机测试模式进入失败或退出未完成

适用模式：fixed_primary_test、maintenance；设备组：local、remote；证据日期：无现场采集日期。

历史案例（anonymous_example），不代表当前设备状态。
匿名示例资料；不是历史修复成果或当前现场状态。

<!-- record:HA-006:historical_finding -->
示例固定VIP未释放前恢复备机选举可能造成冲突。
<!-- /record:HA-006:historical_finding -->

<!-- record:HA-006:historical_outcome -->
读取阶段与控制权，失败保留状态；不保证断网后自动回滚。
<!-- /record:HA-006:historical_outcome -->

## ppp5无载波无地址，隔离尝试后仍未恢复

适用模式：normal_ha；设备组：remote；证据日期：无现场采集日期。

历史案例（anonymous_example），不代表当前设备状态。
匿名示例资料；不是历史修复成果或当前现场状态。

<!-- record:HA-007:historical_finding -->
无载波与IP不足以认定DHCP损坏；这是构造的未解决样例。
<!-- /record:HA-007:historical_finding -->

<!-- record:HA-007:historical_outcome -->
未修复；示例未执行重拨、AT指令或设备操作。
<!-- /record:HA-007:historical_outcome -->

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

## 单向有线路径可达，回程路由缺失

适用模式：normal_ha；设备组：remote、local；证据日期：无现场采集日期。

历史案例（anonymous_example），不代表当前设备状态。
合成演示案例。

<!-- record:HA-009:historical_finding -->
合成路由表显示回程缺少到示例源网段的路由。
<!-- /record:HA-009:historical_finding -->

<!-- record:HA-009:historical_outcome -->
结合选路与回程判断影响范围。
<!-- /record:HA-009:historical_outcome -->

## PPP 有地址，业务回程仍缺失

适用模式：normal_ha；设备组：remote、local；证据日期：无现场采集日期。

历史案例（anonymous_example），不代表当前设备状态。
合成演示案例。

<!-- record:HA-010:historical_finding -->
模拟环境中局端与 Router 均缺用户网段回程。
<!-- /record:HA-010:historical_finding -->

<!-- record:HA-010:historical_outcome -->
结合选路与回程判断影响范围。
<!-- /record:HA-010:historical_outcome -->

## 运行 NAT 与启动配置不一致

适用模式：normal_ha；设备组：remote、local；证据日期：无现场采集日期。

历史案例（anonymous_example），不代表当前设备状态。
合成演示案例。

<!-- record:HA-011:historical_finding -->
示例启动配置仍引用旧出口的固定 SNAT。
<!-- /record:HA-011:historical_finding -->

<!-- record:HA-011:historical_outcome -->
结合选路与回程判断影响范围。
<!-- /record:HA-011:historical_outcome -->

## 管理入口不可达，业务探测仍有回包

适用模式：normal_ha；设备组：remote、local；证据日期：无现场采集日期。

历史案例（anonymous_example），不代表当前设备状态。
合成演示案例。

<!-- record:HA-012:historical_finding -->
合成示例的共享管理入口不可达，当次内层探测仍收到应答。
<!-- /record:HA-012:historical_finding -->

<!-- record:HA-012:historical_outcome -->
结合选路与回程判断影响范围。
<!-- /record:HA-012:historical_outcome -->

## 普通 ping 失败，指定有线源后成功

适用模式：normal_ha；设备组：remote、local；证据日期：无现场采集日期。

历史案例（anonymous_example），不代表当前设备状态。
合成演示案例。

<!-- record:HA-013:historical_finding -->
普通 ping 使用管理 PPP，指定有线源后匹配另一条策略路由。
<!-- /record:HA-013:historical_finding -->

<!-- record:HA-013:historical_outcome -->
结合选路与回程判断影响范围。
<!-- /record:HA-013:historical_outcome -->
