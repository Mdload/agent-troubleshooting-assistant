# Linux日常操作

整理日期：2026-10-12。以下为通用入门资料，命令示例不是现场执行记录。

## 文件与路径

用途：找到程序、配置和日志，知道命令从哪个目录执行。

基础概念：绝对路径从根目录 `/` 开始；相对路径以当前工作目录为起点。`.` 是当前目录，`..` 是上一级，`~` 通常是当前用户的主目录。文件名大小写可能不同，包含空格的路径应加引号。

常用命令：`pwd` 看当前目录；`ls -lah` 看目录内文件、隐藏文件及权限；`stat sample.log` 看指定文件属性。`cd /path/to/project` 只改变本次终端的工作目录，示例路径需替换为实际路径。

常见问题：`No such file or directory` 先核对工作目录、拼写和文件是否存在。文件存在但打不开，再查权限；不根据旧文档路径断定当前程序安装在同一位置。

参考：[ls手册](https://manpages.debian.org/bookworm/coreutils/ls.1.en.html)、[stat手册](https://manpages.debian.org/bookworm/coreutils/stat.1.en.html)。

## 用户与权限

用途：判断谁能读、写或执行文件，理解普通用户和管理员的职责。

基础概念：Linux权限通常分所有者、所属组、其他用户三类；`r/w/x` 是读、写、执行。目录的 `x` 表示能进入或遍历。`sudo` 按系统授权以其他用户身份运行命令，不等于任何人都能成为root。

常用命令：`whoami`、`id` 查看身份与用户组；`ls -ld /path/to/directory` 查看目录权限；`ls -l sample.py` 查看文件权限。

常见问题：`Permission denied` 可能来自文件、父目录、用户组或其他访问控制。先定位拒绝发生在哪里；把目录统一改成777会扩大权限，不能作为通用修复。权限修改与提权属于另行授权的操作。

参考：[id手册](https://manpages.debian.org/bookworm/coreutils/id.1.en.html)、[chmod手册](https://manpages.debian.org/bookworm/coreutils/chmod.1.en.html)。

## 进程与资源

用途：找到运行中的程序，初步观察CPU、内存和负载。

基础概念：程序启动后成为进程，PID是进程标识；一个服务可能包含多个进程。CPU占用、内存占用和系统负载是不同指标，一次采样不能代表持续压力。

常用命令：`ps -eo pid,comm,%cpu,%mem --sort=-%cpu` 看进程概览；`free -h` 看内存；`uptime` 看运行时间和平均负载；`top -b -n 1` 获取一次资源快照。

常见问题：内存的缓存不应全部当作不可用空间；结合available等字段判断。进程存在只说明它存在，不能证明监听、接口或业务成功。确认高占用进程后先查原因，不直接杀死共享业务。

参考：[ps手册](https://manpages.debian.org/bookworm/procps/ps.1.en.html)、[free手册](https://manpages.debian.org/bookworm/procps/free.1.en.html)。

## 磁盘与文件大小

用途：定位磁盘空间不足，区分分区使用量与某个目录的占用。

基础概念：`df` 看文件系统容量，`du` 估算目录或文件占用；inode用于表示文件对象，空间剩余不代表还能创建无限多文件。

常用命令：`df -h` 看空间；`df -i` 看inode；`du -sh /path/to/known-directory` 看一个明确目录的占用。`du`会扫描目录，先选小范围。

常见问题：`No space left on device` 同时检查空间、inode及实际写入所在文件系统。`df`与`du`不一致可能与挂载、权限或仍被进程占用的已删除文件有关；清理前确认归属和保留要求。

参考：[df手册](https://manpages.debian.org/bookworm/coreutils/df.1.en.html)、[du手册](https://manpages.debian.org/bookworm/coreutils/du.1.en.html)。
