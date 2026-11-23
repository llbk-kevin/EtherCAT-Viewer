# 第三方组件与许可

## 一、EtherCAT 主站库

PySOEM 1.1.13 使用 MIT 许可证。其 Windows wheel 静态编入 SOEM 代码；SOEM 1.4 分支的相应代码使用 GPL-2.0 与链接例外。PySOEM 同版源码发行包包含构建所用的 SOEM 源码与构建配置。

- [PySOEM 1.1.13 对应源码包](https://files.pythonhosted.org/packages/76/32/69116eb84c8f11a64561080a1401187a904afcaee4d7bd4e9717b0a21d4e/pysoem-1.1.13.tar.gz) · [发行页及校验值](https://pypi.org/project/pysoem/1.1.13/#files)
- 源码包 `pysoem-1.1.13.tar.gz` 的 SHA-256 为 `4be98d8531e492f2ee5d21f6ef51e51493a10ee1322ed22e909c91a8bcfff950`。其中 `soem/` 为 SOEM 源码。
- [SOEM 1.4 许可证及例外](licenses/SOEM-LICENSE.txt) · [GPL-2.0 完整许可](licenses/GPL-2.0.txt)

## 二、网页和打包依赖

仪表盘内置 uPlot 1.6.32，版权和许可见 [uPlot 许可证](viewer/static/vendor/LICENSE)。EXE 解包时包含 Python 与随包收集到的依赖许可，许可文件位于运行资源的 `licenses/` 目录。

## 三、系统驱动

实机所需 Npcap 由使用者单独下载和安装。驱动、安装器以及 Npcap DLL 由 [Npcap 官网](https://npcap.com/#download) 提供。

## 四、设备产品图片

离线模块缩略图来自 Beckhoff 官方公开产品资料，图片版权归 Beckhoff Automation 所有，用于在本项目中识别对应真实模块，未修改产品内容。

- EK1100：[官方产品页](https://www.beckhoff.com/en-us/products/i-o/ethercat-terminals/ek1xxx-bk1xx0-ethercat-coupler/ek1100.html) · [图片来源](https://multimedia.beckhoff.com/media/ek1100__web_preview.png)
- EL3104：[官方产品页](https://www.beckhoff.com/en-us/products/i-o/ethercat-terminals/el-ed3xxx-analog-input/el3104.html) · [图片来源](https://multimedia.beckhoff.com/media/el3104__web_preview.png)
- EL4134：[官方产品页](https://www.beckhoff.com/en-us/products/i-o/ethercat-terminals/el-ed4xxx-analog-output/el4134.html) · [图片来源](https://multimedia.beckhoff.com/media/el4134_es4134__web_preview.png)

文件位于 viewer/static/images/modules/，未使用在线运行依赖。
