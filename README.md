# Portfolio Debrief（组合复盘）

每日收盘，把盈亏来源、净值回撤和风险提醒后的实际仓位变化放在同一张表里，让管理者看清账户发生了什么，自己又做了什么。

首版面向主动管理基金经理，读取机构 OMS（订单管理系统）导出的持仓、成交和资产文件，在本机生成 Excel 复盘表。个人投资者是后续目标用户，当前输入格式以机构 OMS 为准。

## Windows 使用方法

[下载 Windows x64 便携包（A3 试用版）](https://github.com/CyberYY2030/portfolio-debrief/releases/download/A3/portfolio-debrief-A3-windows-x64.zip)，完整解压后双击「生成持仓表.cmd」，选择三份 OMS 导出所在目录，即可为每个产品生成一份 Excel。首次可用包内的虚构账户试读。详见 [Windows 使用说明](windows/README.md)。

下载包是 A3 试用版。`main` 分支的源码已更新到 C2：首页的结论收成一行「今日结论」，「今日成交」按日线涨跌幅粗略标注追涨、杀跌。C2 便携包尚未发布，可按下文从源码构建；[Windows 使用说明](windows/README.md)描述的是 C2 的页面。

## 从源码构建便携包

准备 Python 3 和 Python 官方的 Windows x64 嵌入式运行包 `python-3.13.17-embed-amd64.zip`。在仓库根目录运行以下命令（运行包放在当前目录）：

```sh
python windows/build_portable.py --runtime python-3.13.17-embed-amd64.zip --output portfolio-debrief-C2-windows-x64.zip
```

构建脚本校验运行包的 SHA-256，并生成合成试用数据。账户复盘在本机完成。生成时向行情接口请求两类公开行情：沪深300 指数日线；当天有成交且收盘已不持有的证券的日线，这一类会发送证券代码。不发送产品、账户、持仓数量、成交数量或金额。断网时仍可生成复盘表。发送内容和取不到时的处理见 [Windows 使用说明](windows/README.md)。

本版已用合成数据验证经理入口。经理 Windows Excel 实机与连续多日真实账本的验收仍待完成。
