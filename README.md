# EtherCAT Viewer

Windows 10/11 x64 本地仪表盘。支持 Beckhoff EK1100 → EL3104 → EL4134。现场验证接线为 AO1 → AI1、AO2 → AI3、AO4 → AI4，均为双线回环。EL9011 是末端盖。

在项目目录运行启动脚本，打开 [EtherCAT Viewer](http://127.0.0.1:8765/)。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start.ps1
```

首次启动要求 Python 3.12 x64。实机通信需要 [Npcap](https://npcap.com/#download)，安装时选择 WinPcap API-compatible Mode。启动后显示「掉线」。点击「取得控制权」，确认「以太网 3」，再点「连接实机」。网卡更换后，在 `viewer/hardware.py` 更新现场 GUID。启动和重连始终从零值锁定状态开始。

八个图框共用时间窗，上排是四路已获回执的 AO 下发值，下排是四路 AI 采集值。用滚轮或拖拽框选查看历史，点击「回到实时」恢复最近 30 秒的滚动。实时读数旁的电压与图中纵轴均使用 V，时间轴使用 s。

点击「解锁输出」后，四路均支持 −10 至 +10 V 的直流以及正弦、三角、方波。周期波形填写峰值和频率，方波占空比为 50%。单通道「停波清零」只影响该路。换线前用「清零并锁定」，接好后重新解锁。输出的物理电压可用万用表独立核对。关闭控制页或通信故障时撤销输出，重连需重新解锁。

「通信诊断」支持 1–50 ms 的整数目标周期，默认 10 ms。在线切换保留波形相位和直流值。如果任一运行中的波形超出新周期允许的频率，旧设置继续生效。频率下限 0.01 Hz，上限为 100 Hz 和目标更新率的四分之一中的较小值；每个波形周期少于 20 个输出更新点时，曲线可能出现可见台阶。

万用表参考值、增益和偏移保存在当前浏览器。曲线与输出使用设备原始量程换算。退出服务用 Ctrl+C。

连接、WKC、状态码和 PDO 表在「通信诊断」中。周期间隔是相邻两次 PDO 交换起始的间隔，主站收发耗时是单轮发送到接收返回的用时。两项各显示近 10 秒统计。调度漏期、WKC 异常和网页推送遗漏各自统计。WKC（Working Counter，工作计数器）是每轮数据交换的处理回执。页面以实际值 / 预期值显示，例如 3 / 3 表示这一轮读写都得到了确认。连接失败时先确认 100 Mbps 链路、Npcap 兼容模式和选中的接口，再看具体 CoE、PDO 或 AL 状态错误。如果适配器打开失败且 Npcap 限制管理员访问，从管理员 PowerShell 运行启动命令。真实连接由独立通信进程独占，运行零输出诊断时先断开网页实机连接。

```powershell
.\.venv\Scripts\python.exe -X utf8 -m viewer.soak
.\.venv\Scripts\python.exe -m pip install pytest
.\.venv\Scripts\python.exe -m pytest -q
```

`viewer.soak` 默认运行 180 秒零输出通信，保存从站、PDO 映射、看门狗及周期统计到 `logs/zero-soak.json`。网页记录保存到 `recordings/`，停录后可下载 CSV。AI 电压由终端回读计算；WKC 确认的是过程数据交换。使用万用表独立核对实际 AO 电压和断线后的电压。

后台固定 PySOEM 1.1.13 / SOEM 1.4 分支快照。PDO 使用 CoE 实际分配和缓冲区尺寸校验。EL4134 使用默认有符号表示、默认缩放和零看门狗替代值。实机连接时启用并回读 SM2 看门狗触发位，超时目标 100 ms。周期是 Windows 主站的调度目标。以实际周期分布判断表现，实机验收记录包含 1 ms 档的回执超时结果。uPlot 1.6.32 随程序本地提供，版权声明位于对应的 vendor 目录。

[方案审核与现场基线](docs/review.md)

测量 EL4134 时，万用表置直流电压档。AO1 红笔接上半部左列「01」（物理端子 1），黑笔接下方同列「GND」（端子 2）。AO4 红笔接下半部右列「04」（端子 7），黑笔接下方同列「GND」（端子 8）。见 [Beckhoff 接线和 LED 资料](https://infosys.beckhoff.com/content/1033/el41xx/13207030411.html)。
