# 匿名示例资料
这里描述角色与程序关系，不含任何真实管理入口、设备主机名、现场映射或运行快照。

## 设备与程序
| 示例设备 | 示例固定地址 | 主要程序与入口 | 本次资源状态 |
| --- | --- | --- | --- |
| 局端主机 | 192.0.2.13 | Croc_sar、Keepalived | 生成的历史夹具记录：服务处于 active |

四个业务角色为local-primary、local-backup、remote-primary、remote-backup；名称是登记标签，不证明当前主机是谁。
局端使用Croc SAR与Keepalived；远端使用Croc SMR、Keepalived及业务路由单元。备机拨号可常驻而Croc停止。
Router负责转发；示例管理跳板及接入网关只负责管理。此例没有设备实时状态。

## 关键程序路径
以下名称用于示例职责索引。

- 测试节点示例 agent.py 配合控制器记录测试模式。
- 远端启动由 rc.local 调用拨号入口。Croc 由 systemd 和本机角色回调负责启动。
- 远端 remote-ha-candidate.sh 核对候选资格，remote-ha-health.sh 检查角色资源；manage_services.sh 调度示例常驻程序。

远端启动：rc.local 可调用示例拨号入口。当前演示架构中启动 Croc 的职责由 systemd 和 HA 角色回调承担。局端由 notify 启动或停止业务服务。
monitor collector会写采集结果，控制器会改变运行模式，健康及watchdog脚本可能写状态，均不能当作只读工具执行。
示例代码使用 example-croc-sar.service、example-croc-smr.service 和 example-ha-routes.service 对应这三类程序。
看到磁盘旧启动脚本不等于正在运行；数据库和前端业务关系没有采集。

## 角色与网络细节
示例局端Keepalived通过eth2做心跳单播，eth3为业务侧；其他接口有地址也不证明是心跳口。
远端eth4可承担心跳，PPP可承担候选业务链路。角色、VIP、连接和路由是不同观察范围。
名称可重复，身份检查须结合登记ID与虚构固定示例地址；本候选不提供实际注册或管理连接。
