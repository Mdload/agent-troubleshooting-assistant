# 网络与接入配套

本模块的日常知识入口。章节保留各自的模式、日期和适用条件；原始资料及出处单独存档。

## Router

适用模式：normal_ha；设备组：router；证据日期：2026-09-28。

<!-- record:KB-003:historical_finding -->
Router示例同步七个VRRP实例，使用优先级150与100；它负责路由转发及VIP同步，不终止Croc隧道。
模拟 TOR4 交换机故障时，物理 MAC 与 VRRP 虚拟 MAC 要分别核对。设备重启可能触发表项更新，须以复测确认。
只读观察转发开关与路由，不执行可能写状态的检查脚本。接口配置和完整接线属于不同证据。
<!-- /record:KB-003:historical_finding -->
