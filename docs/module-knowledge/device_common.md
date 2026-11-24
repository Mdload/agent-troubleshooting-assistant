# 设备共同知识

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

## 对排障助手的要求

适用模式：按正文说明；设备组：按正文说明；证据日期：2026-09-28。

<!-- record:KB-007:historical_finding -->
三层独立热备中的 PPP 探测应核对指定源地址与策略路由。普通 ping 使用系统默认选路，可能走管理 PPP；绑定业务源地址时需结合对应策略表和回程判断。

先识别设备和模式，区分匿名示例、静态配置、历史记录、合成观测和真实设备证据。
直接读取systemctl属性、ip、ss等有界信息；example-ha-health.sh等健康脚本会写锁、目录、计数或故障标记，不能包装为只读工具。
端口、PID、VIP或一次ping都不足以证明完整业务恢复。解释结论时保留来源、条件和未知项。
<!-- /record:KB-007:historical_finding -->
