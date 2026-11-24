# 远程访问与文件传输

整理日期：2026-10-12。示例目标不代表获准登录任何实验室设备。

## SSH与身份验证

用途：通过加密连接进入登记服务器，或执行已授权的命令。

基础概念：服务器主机密钥用于核对服务器身份；用户密钥或密码用于证明用户身份，二者不同。SSH默认端口通常为22，但实际端口可以配置改变。

命令形式：`ssh -p 22 user@example.net` 中 `-p` 是服务器SSH端口。登录后用 `hostname`、`whoami`、`pwd`、`date -Is` 确认所处环境；示例域名、用户和端口需要替换为已登记信息。

常见问题：`Permission denied (publickey)` 先检查账户、密钥选择和服务器授权；主机密钥变化先核实机器身份，不绕过校验或盲删known_hosts。密码只在交互提示中输入。

参考：[OpenSSH ssh手册](https://man.openbsd.org/ssh)。

## SCP与SFTP

用途：在本机和服务器之间传输文件，理解传输方向和目标路径。

基础概念：SCP提供命令行复制，SFTP提供文件传输会话。访问账户、SSH端口、服务器支持和目标目录权限都会影响传输。

命令示例：`scp -P 22 sample.txt user@example.net:/path/to/authorized-directory/` 是上传；`sftp -P 22 user@example.net` 是进入传输会话。SCP/SFTP端口选项是大写 `-P`，与ssh的 `-p` 区分。

常见问题：传输前检查方向、目录和同名文件；复制可能覆盖目标文件。传完后核对大小，必要时比较双方 `sha256sum sample.txt`，传输退出码成功不替代内容完整性核对。

参考：[OpenSSH scp手册](https://man.openbsd.org/scp)、[OpenSSH sftp手册](https://man.openbsd.org/sftp)。

## SSH端口转发

用途：从本机访问仅在服务器回环地址监听的网页或接口。

基础概念：本地转发把本机端口经SSH连接送到远端指定目标。本机的127.0.0.1和服务器的127.0.0.1属于不同机器；通道依赖SSH进程存活。

学习示例：`ssh -NT -L 127.0.0.1:18080:127.0.0.1:8080 user@example.net` 把本机18080转到SSH服务器视角的回环8080。`-N`不执行远程命令，`-T`不分配终端。

常见问题：网页打不开时先看本机端口是否监听、SSH是否仍运行，再查远端服务。`Address already in use` 表示绑定端口被占用，不能直接结束占用进程。转发正常只证明访问通道，不证明模型或下游业务正常。

参考：[OpenSSH ssh手册](https://man.openbsd.org/ssh)、[OpenSSH客户端配置手册](https://man.openbsd.org/ssh_config)。

## 代理与HTTPS

用途：区分直连、显式代理和代理规则，定位HTTPS连接失败。

基础概念：使用代理会改变请求经过的路径。HTTPS还需要完成TLS并验证证书身份；请求成功、鉴权成功和模型生成成功是不同层次。程序环境变量和代理配置可能影响出口。

检查顺序：记录失败时间和目标域名，区分DNS、连接、TLS、HTTP与鉴权阶段；核对是否走代理、匹配哪条规则、配置是否只在运行时生效。经批准的无密钥HEAD只验证HTTPS入口，不调用模型生成。

常见问题：证书域名不匹配需要核对目的地和代理节点；不通过关闭证书验证来修复。临时规则可能在代理重启或重载后丢失，永久规则要放到该软件真正加载的配置入口。`model_connection_failed`是应用的失败类别，根因还需网络证据。

参考：[OpenSSH代理配置手册](https://man.openbsd.org/ssh_config)、[HTTP语义RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html)。
