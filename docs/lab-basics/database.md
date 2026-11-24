# 数据库基础

整理日期：2026-10-12。示例表和字段是虚构的，未读取任何实验室数据库。数据库访问按单独批准范围进行。

## 数据库与表

用途：理解程序如何按结构保存数据，知道查询对象是什么。

基础概念：表由行与列组成，字段通常对应列，记录通常对应行；数据类型约束值的表示。数据库、模式/schema和账户的关系因产品而异，不能把MySQL的全部用法直接套到达梦。

基础核对：先确认产品、版本、登录身份及当前数据库/模式，再确认表和字段。MySQL中 `SELECT DATABASE();` 可查看当前数据库；其他产品使用其对应接口，不猜测实验室库名。

常见问题：程序进程存在不证明数据库连接成功；看到MySQL进程也不能推出哪个应用使用了哪张表。实际用途与依赖需配置或可靠资料支持。

参考：[MySQL官方入门](https://dev.mysql.com/doc/refman/8.4/en/tutorial.html)、[达梦查询手册](https://eco.dameng.com/document/dm/zh-cn/pm/check-phrases.html)。

## SELECT与条件查询

用途：从指定表读取满足条件的数据，控制输出范围。

基础概念：SELECT选列，FROM指定表，WHERE筛选条件，ORDER BY排序。字符串条件与时间范围应按实际字段类型处理。SELECT FOR UPDATE会加锁，不属于本项目允许的普通只读查询。

虚构示例：`SELECT id, value FROM sample_metrics WHERE id = 1;`。MySQL可用 `SELECT id, value FROM sample_metrics ORDER BY id LIMIT 10;` 限制行数；达梦的行数限制与语法请按实际版本手册核对。

常见问题：少了WHERE可能查询大量数据；条件字段名和类型不符可能报错。日期比较要核对时区和字段类型；只查必要列和小范围，不把未知值当0，也不读取无关业务数据。

参考：[MySQL SELECT语法](https://dev.mysql.com/doc/refman/8.4/en/select.html)、[达梦查询手册](https://eco.dameng.com/document/dm/zh-cn/pm/check-phrases.html)。

## 数据库连接问题

用途：区分网络连接、账户认证、库表权限及程序配置问题。

基础概念：客户端需要正确的地址、端口、账户和目标数据库/模式。连接超时、拒绝连接、Access denied和对象不存在是不同失败阶段，不能只凭“连不上”确定根因。

检查顺序：记录完整但脱敏的错误类别；先核对已登记的地址、监听和客户端参数，再核对账户与授权。数据库操作前由用户进入获准的客户端，密码不写进命令参数或聊天。

常见问题：同名账户从不同来源连接可能受不同授权约束。应用连接失败不代表整个数据库停止；实际端口、库名、账户和权限需要本项目配置证据，通用教程不能确认。

参考：[MySQL连接与断开说明](https://dev.mysql.com/doc/refman/8.4/en/connecting-disconnecting.html)。

## 备份与导出

用途：理解可恢复备份、数据导出和SQL文件之间的区别。

基础概念：逻辑导出记录结构或数据，物理备份处理数据库存储文件；恢复方式、版本兼容、一致性和账户权限不同。一个SQL文件可能包含创建或写入语句，不能把打开SQL文件等同于只读操作。

入门命令：`mysqldump --help` 查看MySQL导出工具选项；实际导出前确认库/表范围、一致性要求、保存位置和敏感字段。达梦使用对应产品的备份工具，不照搬mysqldump。

常见问题：导出完成不等于已验证可以恢复；恢复会改变数据库，须独立授权并先在隔离环境验证。导出文件按业务数据保护，不放入公开Git仓库或发送给未获准的外部服务。

参考：[MySQL mysqldump手册](https://dev.mysql.com/doc/refman/8.4/en/mysqldump.html)。
