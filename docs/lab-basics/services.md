# 程序与服务

整理日期：2026-10-12。介绍通用机制，不登记实验室实际单元名、端口或程序关系。

## systemd服务状态

用途：了解服务管理器报告的状态，区分服务与进程。

基础概念：systemd的service单元描述程序如何启动和管理。active/inactive/failed是较高层状态，SubState提供细分信息；oneshot任务可执行完成后保持active/exited，不一定有持续进程。

常用命令：`systemctl status example.service --no-pager`；`systemctl show example.service --property=ActiveState,SubState,MainPID`。`example.service`只是占位单元名。

常见问题：active不表示HTTP或业务已经可用，MainPID也不代表所有子进程。需要结合单元类型、日志及实际接口判断。检查范围内没有读取某项，就不能说该项已经正常。

参考：[systemctl手册](https://manpages.debian.org/bookworm/systemd/systemctl.1.en.html)、[systemd.service手册](https://manpages.debian.org/bookworm/systemd/systemd.service.5.en.html)。

## 启动与开机自启

用途：理解当前运行、开机启动及实际启动入口之间的区别。

基础概念：enabled通常说明已配置某种启动链接，不等于服务当前运行；disabled也不等于从未运行。服务还可能由依赖、timer、socket、脚本或用户操作触发。系统服务与 `--user` 用户服务应分别核对。

常用命令：`systemctl is-enabled example.service`；`systemctl cat example.service` 看单元和覆盖配置；`systemctl list-timers --all --no-pager` 看定时触发关系。读取单元时留意凭据，不公开Environment中的敏感值。

常见问题：重复启动可能造成端口冲突或重复进程，先确认已有实例。磁盘上存在rc.local或旧脚本不证明它仍负责启动。start/stop/restart/enable会改变状态，不能把它们当成只读查询。

参考：[systemctl手册](https://manpages.debian.org/bookworm/systemd/systemctl.1.en.html)。

## 日志与时间

用途：按时间和服务定位异常，避免把不同机器的日志误对齐。

基础概念：日志时间可能带时区，也可能来自时钟有偏差的设备。程序日志与采集端时间不是同一个概念；时间接近只能支持关联，不能单独证明因果。

常用命令：`journalctl -u example.service -n 50 --no-pager` 读取有限条目；`journalctl -u example.service --since '10 minutes ago' --no-pager` 限制时间；`date -Is` 查看当前时间表示。普通文件可用 `tail -n 50 sample.log`。

常见问题：空日志可能是时间范围、单元名、权限或日志存储方式不符，不直接证明没有异常。跨机记录保留原时间、时区和采集时间；日志中的密码、令牌和用户数据要脱敏。

参考：[journalctl手册](https://manpages.debian.org/bookworm/systemd/journalctl.1.en.html)。

## HTTP与nginx及Node

用途：理解浏览器、Web服务器、后端程序及数据库的常见分工。

基础概念：HTTP是请求/响应协议；nginx常用于静态内容或反向代理；Node是可运行JavaScript的环境，可承载后端服务。这是常见架构示例，不能据此断言实验室每个进程之间存在这种关系。

常用核对：确认请求URL、状态码、响应内容和对应日志。`ss -ltn`看监听；经过授权可用 `curl -I --max-time 5 https://example.net/` 查看支持HEAD的入口。HTTP常用80、HTTPS常用443，但应用可以配置其他端口。

常见问题：401表示身份认证相关拒绝，404表示未找到对应资源，502常涉及网关收到无效上游响应；原因需结合日志。200只说明该请求获得成功状态，不证明全部功能或模型问答可用。

参考：[nginx入门](https://nginx.org/en/docs/beginners_guide.html)、[Node官方介绍](https://nodejs.org/learn/getting-started/introduction-to-nodejs)、[HTTP语义RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html)。
