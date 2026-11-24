# 脚本与开发环境

整理日期：2026-10-12。只介绍基础使用，安装、文件写入和提交在操作者获准的工作区进行。

## Python脚本与报错

用途：理解一个小脚本的输入、处理和输出，定位运行失败。

基础概念：Python解释器执行脚本，import加载模块，函数处理输入并返回结果。脚本中的代码可能访问文件或设备，运行前要看它实际做什么，名称含check不保证只读。

基础命令：`python --version` 核对解释器版本；`python sample.py`运行已审阅的小实验；出现Traceback时先看异常类型和最后的出错位置，再沿调用链寻找原因。

常见问题：ModuleNotFoundError/ImportError可能与模块名、搜索路径、工作目录或解释器环境有关，不能一律归结为没安装包。文件命名成json.py等也可能遮蔽标准库。报错输出要先去掉凭据再分享。

参考：[Python异常入门](https://docs.python.org/3/tutorial/errors.html)、[Python模块入门](https://docs.python.org/3/tutorial/modules.html)。

## 虚拟环境与依赖

用途：把项目依赖与其他项目、系统Python隔离，定位包安装到了哪个解释器。

基础概念：venv创建独立环境；pip安装包，`python -m pip`明确使用该python对应的pip。环境隔离不自动保证代码安全或所有包兼容。

学习命令：`python -m venv .venv`会创建目录；Linux可 `source .venv/bin/activate`。Windows也可直接用 `.\.venv\Scripts\python.exe -m pip --version` 查看对应pip。`python -c "import sys; print(sys.executable)"`确认解释器路径。

常见问题：终端、IDE和服务可能用不同解释器，先核对路径再考虑安装。安装依赖会改变环境，需要确定版本和工作区；不要为了修一个项目升级共享服务器的系统Python。

参考：[Python venv文档](https://docs.python.org/3/library/venv.html)、[Python包安装说明](https://docs.python.org/3/installing/index.html)。

## JSON与接口请求

用途：理解脚本、网页和服务之间的数据交换。

基础概念：JSON表示对象、数组、字符串、数字、布尔值和null；与Python字典的写法不同。HTTP状态、响应头、正文和JSON字段应分别核对。

小例子：JSON `{"ok":true,"count":2}` 经 `json.loads`变成Python对象；`json.dumps`把对象编码为JSON文本。`python -m json.tool sample.json`可验证一个已脱敏的示例文件，不能用eval解析外部数据。

常见问题：JSONDecodeError可能因为返回的是HTML、空正文或非法JSON；先核对状态码与内容类型。200不保证业务字段成功；401、超时和模型失败应保留各自类别，不输出请求密钥或完整敏感响应。

参考：[Python json文档](https://docs.python.org/3/library/json.html)、[HTTP语义RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html)。

## Git与版本管理

用途：记录代码变化，比较差异并形成可审阅的提交。

基础概念：工作区是文件现状，暂存区是准备提交的内容，commit形成版本记录；提交与推送是不同操作。未跟踪文件还没有进入版本历史。

常用顺序：先 `git status --short`、`git diff` 查看范围；确认后 `git add -- path/to/file.py`只暂存指定文件；再 `git commit -m "Describe the change"`。add和commit会改变Git状态，由操作者确认后执行。

常见问题：项目可能在更大的父仓库里，先用 `git rev-parse --show-toplevel` 确认根目录，避免一次暂存全部无关文件。密钥、私有模型配置和业务导出不进Git；已有他人改动不覆盖。

参考：[git status](https://git-scm.com/docs/git-status)、[git add](https://git-scm.com/docs/git-add)、[git commit](https://git-scm.com/docs/git-commit)。
