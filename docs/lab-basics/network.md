# 网络基础

整理日期：2026-10-12。地址与命令是示意，不能替代实验室当前配置。

## IP与路由

用途：理解本机地址、子网、网关和数据包选择出口的关系。

基础概念：IP地址标识网络接口；前缀长度用于判断地址所属网络。目的地不在直连网络时，通常需要路由下一跳。默认路由是没有更具体匹配时使用的候选；策略路由还可能考虑源地址等条件。

常用命令：`ip -br addr` 看地址；`ip route show` 看主路由表；`ip rule show` 看策略规则；`ip route get 192.0.2.1` 查看指定示例目标的路由选择，不发送业务测试流量。

常见问题：接口有地址不证明目标可达。存在默认路由也不证明特定源地址选对了出口；需要检查相应规则和路由表。`192.0.2.1`在这里是示例地址，不是实验室登记设备。

参考：[Red Hat网络配置手册](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html-single/configuring_and_managing_networking/index)。

## DNS与域名

用途：把服务域名与IP连接问题分开排查。

基础概念：DNS提供名称解析；解析出IP、TCP连接建立、TLS验证和HTTP响应是不同环节。一个域名可能解析为多个地址，结果可能随时间变化。

常用命令：Linux上可用 `getent hosts example.net` 查询系统名称解析结果；有相应工具时用 `nslookup example.net` 查看DNS回答。`example.net`是示例域名。

常见问题：域名访问失败而IP可连接时，DNS是一个检查方向，但HTTPS证书和虚拟主机也依赖域名，直接改用IP不一定等价。不要因一次失败就改全局DNS，先确认解析来源和失败阶段。

参考：[DNS基础规范RFC 1034](https://www.rfc-editor.org/rfc/rfc1034.html)、[Red Hat网络配置手册](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html-single/configuring_and_managing_networking/index)。

## TCP与UDP及端口

用途：分清主机可达、传输连接与应用服务可用。

基础概念：TCP提供可靠、有序的字节流；UDP发送数据报，不提供TCP式的可靠传输保证。端口用于区分传输层端点，同一个数字的TCP端口和UDP端口不是同一个端点。

常用命令：`ss -ltn` 看TCP监听；`ss -lun` 看UDP套接字；`ss -tn` 看TCP状态。LISTEN是监听状态，ESTAB是连接状态，不能据此确认应用正确返回。

常见问题：`Connection refused` 与超时应分开记录，可能涉及未监听、主动拒绝或路径问题；不能仅凭报错锁定原因。ping通也不证明目标应用端口可用。实际端口要查该服务配置，不能把另一程序的默认端口套过来。

参考：[TCP规范RFC 9293](https://www.rfc-editor.org/rfc/rfc9293.html)、[UDP规范RFC 768](https://www.rfc-editor.org/rfc/rfc768.html)。

## NAT与回程

用途：理解私网访问外部服务以及请求、响应的双向路径。

基础概念：常见私有IPv4范围包括10/8、172.16/12和192.168/16。NAT改变地址或端口映射；响应还需要正确路由以及适用的映射或连接状态。管理路径和业务路径可能经过不同出口。

常用检查：分别画出请求和响应经过的节点，核对目标网段、源地址、下一跳以及在哪一跳发生地址转换。`ip route get 192.0.2.1`只说明本机对该目标的路由选择，不能证明其他节点的回程。

常见问题：单向有包可能与回程、过滤或转换有关，也可能是应用未回应。先取得双向证据，再提出修改转发、NAT或防火墙的方案；这些修改会影响整机网络。

参考：[私有地址RFC 1918](https://www.rfc-editor.org/rfc/rfc1918.html)、[Red Hat网络配置手册](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html-single/configuring_and_managing_networking/index)。
