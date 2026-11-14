# Windows + SOEM + Beckhoff EK1100/EL3104/EL4134 演示实施计划（交给 Codex）

## 0. 目标与范围

在普通 Windows x64 PC 上，使用独立的 USB 有线以太网网卡直连 Beckhoff EK1100 的 X1 IN，通过 SOEM 实现 EtherCAT 主站通信，并用轻量 Python GUI 展示 4 路 EL3104 的 ±10V 模拟量输入曲线与数值；如经验证可靠，再允许安全受控地设置 EL4134 的 4 路 ±10V 模拟输出，实现 AO->AI 回环展示。

这是短期实验演示，不做工业级功能安全、不追求确定性 1ms、不需要 TwinCAT、LabVIEW、PLC 或硬件主站卡。首要目标是能扫描设备、进入 OP、持续读数、显示变化；其次是输出和回环。没有硬件时要支持仿真界面，但不能把模拟输出伪装成真实通信成功。

## 1. 已核实的硬件

按实际照片，顺序是：

- EK1100：EtherCAT 耦合器；X1 IN 接主站，X2 OUT 留空；24V DC Us（耦合器供电）必接，Up（现场电源触点）本次演示可以先不接。
- EL3104：4 通道差分 ±10V 模拟电压输入，16-bit（含符号），通过 E-bus 供电；标准 PDO 可含每通道状态字和测量值，Compact PDO 仅有测量值；具体以运行时扫描结果为准。
- EL4134：4 通道 ±10V 模拟电压输出，16-bit（含符号），通过 E-bus 供电；4 路输出共用模拟 GND；负载应 >5kΩ；默认需保持硬件看门狗开启。
- EL9011：最右端 E-bus 盖板，不是有效 IO 从站，也不应期待它拥有 PDO。

装配：断电情况下从左到右紧密卡在同一根 DIN 导轨，连接 E-bus 侧面触点；顺序 EK1100 -> EL3104 -> EL4134 -> EL9011。

功率预算：两片 EL 模块典型 E-bus 电流约 130mA+190mA=320mA，小于 EK1100 的 E-bus 2A 额定能力。准备可限流的 24V 实验电源。务必依照机身 24V/0V 标签接 Us，别把 USB 网卡当作 PoE 电源。

**EL3104 前端接线**（差分输入，必须两线接到相应端子）：

| 通道 | 输入正 | 输入负 |
|---|---|---|
| AI1 | 1 | 2 |
| AI2 | 5 | 6 |
| AI3 | 3 | 4 |
| AI4 | 7 | 8 |

**EL4134 输出接线**（所有输出 GND 内部共地）：

| 通道 | 输出 | GND |
|---|---|---|
| AO1 | 1 | 2 |
| AO2 | 5 | 6 |
| AO3 | 3 | 4 |
| AO4 | 7 | 8 |

推荐回环：EL4134 AO1 端子 1 -> EL3104 AI1+ 端子 1；EL4134 GND 端子 2 -> EL3104 AI1- 端子 2；其余先悬空。不要把 AO 电压直接接到 EK1100 24V 电源或把 AO GND 当成 PE / Us 0V。上电先确认 AO=0V，再接通模拟信号。

资料：
- EK1100 https://www.beckhoff.com/en-en/products/i-o/ethercat-terminals/ek1xxx-bk1xx0-ethercat-coupler/ek1100.html
- EL3104 https://www.beckhoff.com/en-en/products/i-o/ethercat-terminals/el-ed3xxx-analog-input/el3104.html
- EL4134 https://www.beckhoff.com/en-en/products/i-o/ethercat-terminals/el-ed4xxx-analog-output/el4134.html
- EL9011 https://www.beckhoff.com/en-en/products/i-o/ethercat-terminals/el-ed9xxx-system/el9011.html
- EL3104 接线 https://infosys.beckhoff.com/content/1033/el31xx/12044928907.html
- EL4134 接线 https://infosys.beckhoff.com/content/1033/el41xx/13207030411.html
- EL31xx 过程数据 https://infosys.beckhoff.com/content/1033/el31xx/1710364299.html
- EL4134 signed 数值表示 https://infosys.beckhoff.com/content/1033/el41xx/15553386891.html

## 2. 技术栈与架构

- SOEM 正版上游项目：https://github.com/OpenEtherCATsociety/SOEM
- Windows packet capture/injection：Npcap（驱动）及 Npcap SDK（构建时使用）；兼容模式、管理员权限按 SOEM 的 Windows 适配需要选择。不要依赖普通 TCP/IP Socket 或 IP Ping 发现 EtherCAT。
- C/C++17 + CMake + Visual Studio C++ Build Tools，x64。
- GUI：Python 3.12+、PySide6、pyqtgraph、numpy，桌面窗口。
- 两个进程：`ethercat_backend.exe` 负责 SOEM 扫描/周期通信；`gui.py` 负责曲线。用 localhost TCP 或 JSON Lines 管道传输数据，选择最省事且不会让 GUI 阻塞 EtherCAT 周期线程的一种；状态、错误写 stderr/日志，不与机器数据混流。
- 建议起始 EtherCAT 周期 10ms，GUI 刷新 20Hz，成功后尝试 5ms / 1ms。Windows USB NIC 不能被宣传为严格 1ms 硬实时。
- C++ 后台线程持有唯一 SOEM context，所有 EtherCAT API 调用串行化，GUI 不直接跨线程操作 SOEM；对外发布快照。

注意：上游 SOEM master 目前存在 2.x API 与 CMake 变化，不能机械复制旧版 `ec_init`/`ec_slave` 教程。开工先检查当前 checkout 的 CMakeLists、samples、headers 和 Windows 移植目录；固定一个能够成功构建的 commit/标签，在 README 记录。2.x 的 CMake 最低版本要求可能高于旧文章中的版本；不要将 SOEM 1.x 示例与 2.x API 混写。

## 3. 阶段执行顺序（禁止跨阶段假成功）

### 阶段 A：Windows 环境 / 网卡验证

1. 检查 Windows 版本、x64、VS C++ 工具集、CMake、Python。
2. 安装 Npcap；根据编译条件安装 Npcap SDK 和适当的 WinPcap 兼容模式。
3. 列出 Npcap 可访问的网卡 GUID、名称、MAC、up/down 状态。UI/命令行允许指定 USB 网卡，不能自动选 Wi-Fi。
4. 检查 EK1100 24V Us 与 X1 link；USB 网卡显示 100Mbps linked 不代表 EtherCAT 主站工作。
5. 不配置从站 IP，也不要用 ping 判断是否找到从站。

**验收 A**：列出 USB 有线网卡，打开原始帧接口没有权限/驱动错误；能观察到物理链路。

### 阶段 B：SOEM 官方样例扫描

1. 克隆 SOEM 官方仓库，固定当前稳定可用 commit/标签，按官方当前 Windows 说明构建。
2. 运行上游 samples 中的 adapter enumeration、simple_test / slaveinfo 对应例程，优先记录原始日志。
3. 识别 EK1100、EL3104、EL4134。EL9011 是被动端盖，不应计入可读写 IO 从站。
4. 输出每个从站 name、configured address、vendor ID、product ID、revision、AL state、DC 支持、Ibits/Obits、PDO assignments、bytes offset、ExpectedWKC。
5. 如果失败，分层定位：电源 -> 网卡/Npcap -> EtherCAT discovery -> AL state -> PDO/Watchdog，严禁绕过错误打印假数据。

**验收 B**：扫描到与实物相符的 3 个有效 EtherCAT 设备（具体条目以实测为准），且可以记录真实信息和状态。

### 阶段 C：读写 PDO

1. 基于扫描得到的 SII/CoE/SM/FMMU 配置建立 IOmap；不要假定 EL3104 4×16bit 的值直接紧凑排列。标准 process image 往往每通道同时包含 status 和 signed value；Compact 仅 value。
2. 首先保留设备默认 PDO 配置，打印逐通道实际 byte/bit offset、数据类型、状态字位置。若需要变更 PDO，依照官方 ESI/CoE 手册，并再次扫描验证。不能未经核实地写硬编码 offsets。
3. 尝试转换到 SAFE_OP、OP；在切 OP 前初始化 EL4134 的输出缓冲为 0；处理周期通信、WKC、状态恢复和断线清理。
4. 解码 EL3104 signed 16-bit 原始值，缺省可按正量程 `V ≈ raw * 10 / 32767`、负量程约 `raw * 10 / 32768` 的近似规则，但必须使用官方量程与设备的 representation/scale 设置核实，并保留 raw 数值用于诊断。
5. EL4134 signed 16-bit 输出同样映射 ±10V，默认 0V；限定 GUI 控制范围为 ±1V（可在设备确认后调整），任何异常/退出请求都将输出归零，保持硬件看门狗启用；若通信丢失要明确记录而不是认为软件归零一定成功。
6. 周期默认 10ms，保存 WKC/expectedWKC、连续失败数、state、样本时间戳、丢包统计，失败时尝试合规恢复；不要在循环中重型日志刷屏。

**验收 C**：OP 状态稳定，输入电压随已知信号变化，输出 0 / +1 / -1V 用万用表能确认；任意 PDO 读取失败有可辨识错误而非“看起来正常”。

### 阶段 D：GUI 与曲线

GUI 显示：
- 左上：网卡选择、连接/断开、Device status、OP状态、Cycle period、WKC/expectedWKC。
- 中间：4 路 AI 电压的滚动曲线（最近 10~30s），支持通道显隐与自动量程。
- 右侧：AI1~AI4 的电压数值、原始 INT16、status flags。
- 下方：4 路 AO 的输出设置（默认禁用，须经确认才解锁）；0V 清零按钮。
- 可选：本地 CSV 记录，包含本机单调时间戳、四路 AI 原始值/电压、AO 命令值、WKC 状态。

先做 simulation mode（4 路正弦/阶跃数据，用于 GUI 独立验收），再接真实 SOEM backend；必须醒目标注 SIMULATION 或 LIVE。

**验收 D**：一条命令启动 UI，可看到数据滚动；断开网线后能明确报错、停止输出控制、可重新连接；界面不冻结。

### 阶段 E：AO -> AI 实验

1. 使用双线短接方式把 AO1->AI1+、AO1 GND->AI1-（仅确认断电且输出为 0 的条件下接线）。
2. 以 0V 起步，运行后改变 AO1 为 +1.0V、-1.0V，检查 AI1 曲线和万用表测量是否大致一致。
3. 成功后展示 0.2Hz 正弦波或阶跃，仅用 ±1V 小信号，优先展示可见闭环，不追求高采样速率。
4. 若一切稳定，尝试周期从 10ms 逐级降到 5ms / 1ms；记录一段运行中丢帧和最长周期抖动，但不以硬实时作为演示合格条件。

**验收 E**：真实 AO1 输出、AI1 回读、屏幕曲线变化能够相互验证，不靠模拟数据。

## 4. 项目目录建议

```
EtherCAT-Windows-Demo/
  README.md
  docs/
    hardware_wiring.md
    discovery_and_pdo.md
    troubleshooting.md
  third_party/
    SOEM/                 # 使用 submodule 或固定 commit
  backend/
    CMakeLists.txt
    main.cpp
    ethercat_worker.cpp
    ethercat_worker.h
    protocol.md
  frontend/
    requirements.txt
    gui.py
    simulation.py
  tests/
    test_scaling.py
    test_mock_protocol.py
  scripts/
    install_python.ps1
    run_demo.ps1
  logs/
```

## 5. 面向 Codex 的工作纪律

- 先保证官方下载/构建/扫描，再加 GUI；不要上来同时写几千行前后端。
- 任何真实硬件输出必须有用户明确解锁。后台退出前请求置零，保留 watchdog；离线/断线时禁用 AO 控制 UI。
- 先检查版本/README，不能按旧 SOEM 1.x API 猜测最新 2.x 的函数签名。
- 每步留下真实的构建日志、设备扫描结果、WKC、PDO offset；不要伪造硬件测试成功。
- 无法访问实体硬件时用 simulation/mock 完成 GUI 测试，明确标识哪些项目等待用户现场测试。
- 完成后给出 Windows 上复制粘贴可运行的命令以及故障排查步骤。不要强制安装 TwinCAT 或控制 PLC。

## 6. 参考网页

- SOEM 官方仓库：https://github.com/OpenEtherCATsociety/SOEM
- Npcap 官方：https://npcap.com/
- Npcap 开发说明：https://npcap.com/guide/npcap-devguide.html
- PyQtGraph：https://www.pyqtgraph.org/
- EK1100：https://infosys.beckhoff.com/content/1033/ek110x_ek15xx/7636174091.html
- EL3104：https://infosys.beckhoff.com/content/1033/el31xx/12044928907.html
- EL31xx PDO：https://infosys.beckhoff.com/content/1033/el31xx/1710364299.html
- EL4134：https://infosys.beckhoff.com/content/1033/el41xx/13207030411.html
- EL4134 raw scaling：https://infosys.beckhoff.com/content/1033/el41xx/15553386891.html


## 7. 现场组装与接线记录（用户口述，2026-11-13）

> 本节记录当前实物接线状态，优先级高于前文“推荐接线”的初始假设。以下内容是用户口述记录，尚未由现场照片或万用表复核。

### 7.1 端子排顺序

从左到右，当前已组装的顺序为：

1. **EK1100**：EtherCAT 耦合器；
2. **EL3104**：4 通道模拟量输入；
3. **EL4134**：4 通道模拟量输出。

用户本次未提及是否在最右侧安装 EL9011 端盖，现场需确认端子排末端是否有正确的总线端盖。

### 7.2 EK1100 电源接线（按用户口述）

- EK1100 上方标记为 **24 V / 0 V（Us，Coupler supply）** 的一组端子，已接入 **20 V DC / 0 V**。
- EK1100 下方标记为 **+ / +、− / −（Up，Input for power contacts）** 的电源触点输入，也已接入 **20 V DC / 0 V**；按用户口述，正极接 +20 V，负极接 0 V。

**重要的电源规格提醒：** EK1100 的 Us 额定供电为 24 V DC，允许范围为 **24 V × (1 − 15%) = 20.4 V 至 24 V × (1 + 20%) = 28.8 V**。目前上方 Us 接入的 20.0 V 比官方下限 20.4 V 低 0.4 V，已经略微超出规格范围。正式进行 EtherCAT 通信测试前，建议断电后把电源调整到 **24.0 V DC 标称值**，并在 EK1100 的 Us 端子处实测确认电压；不要只看电源面板设定值。

**必须区分 Us 与 Up：**

- 上方 Us 为耦合器工作电源，并由耦合器内部产生 5 V E-bus 电源，供右侧 EL 端子的电子电路使用。
- 下方 Up 是送入 power contacts 的现场电源母线，供那些明确从 power contacts 取电的端子/现场负载使用；它不是所有 EL 模块电子电路的通用供电输入。
- EK1100 的 Us 与 power contacts 属于不同电位组，官方给出的隔离为 500 V。
- 本项目的 EL3104 和 EL4134 电子电路均由 E-bus 供电；EL4134 的模拟输出也由 E-bus 供电。两者的技术数据均标注 power-contact current consumption 为无/不使用。因此，对当前这两片模拟量模块而言，下方 Up 不是它们工作的必需供电。
- 当前 Up 端已接入 20 V；在确认 +/− 极性无误、没有误接 PE 或其他端子的前提下，可记录为已接入状态。若没有其他需要 power contacts 的模块或现场负载，也可以在断电后将 Up 断开，减少不必要的现场电源连接。Up 的额定上限为 24 V DC，不得接入高于 24 V 的电压。

参考：EK1100 技术数据及 Us/Up 说明：
- https://infosys.beckhoff.com/content/1033/ek110x_ek15xx/7636174091.html
- https://infosys.beckhoff.com/content/1033/ek110x_ek15xx/1880428299.html
- https://infosys.beckhoff.com/content/1033/ek110x_ek15xx/1880439691.html
- EL3104 技术数据：https://infosys.beckhoff.com/content/1033/el31xx/12044045835.html
- EL4134 技术数据：https://infosys.beckhoff.com/content/1033/el41xx/13206933771.html

### 7.3 模拟量输入/输出回环接线（按用户口述）

当前不是只接 AO1→AI1，而是同时接了第 1 通道和第 4 通道：

| 回环 | EL4134 模拟输出端 | EL3104 模拟输入端 | 当前状态 |
|---|---|---|---|
| 通道 1 | AO1：端子 1（Output 1）→ 端子 2（GND） | AI1：端子 1（+Input 1）← 端子 2（−Input 1） | 已连接，双线对应连接 |
| 通道 2 | AO2：端子 5/6 | AI2：端子 5/6 | 悬空，未接线 |
| 通道 3 | AO3：端子 3/4 | AI3：端子 3/4 | 悬空，未接线 |
| 通道 4 | AO4：端子 7（Output 4）→ 端子 8（GND） | AI4：端子 7（+Input 4）← 端子 8（−Input 4） | 已连接，双线对应连接 |

具体连线记录：

- **EL4134 AO1 端子 1 → EL3104 AI1+ 端子 1**；**EL4134 AO1 GND 端子 2 → EL3104 AI1− 端子 2**。
- **EL4134 AO4 端子 7 → EL3104 AI4+ 端子 7**；**EL4134 AO4 GND 端子 8 → EL3104 AI4− 端子 8**。
- 通道 2、3 的输入和输出接线均悬空。

注意：EL3104 的通道编号与物理端子编号并非简单连续排列：AI1=1/2、AI3=3/4、AI2=5/6、AI4=7/8；EL4134 的 AO1=1/2、AO3=3/4、AO2=5/6、AO4=7/8。软件 PDO 中的通道顺序必须根据官方 PDO 映射确认，不得仅凭物理端子排列推断。

参考接线：
- EL3104：https://infosys.beckhoff.com/content/1033/el31xx/12044928907.html
- EL4134：https://infosys.beckhoff.com/content/1033/el41xx/13207030411.html

### 7.4 对 SOEM 后台和 GUI 测试的直接要求

1. 设备识别顺序应与实物相符：EK1100、EL3104、EL4134；EL9011 若存在则是无 PDO 的被动端盖，不应当作普通 IO 从站。
2. **AI1 应跟随 AO1，AI4 应跟随 AO4**。测试时先将 AO1、AO4 都设为 0 V，再分别施加小幅度测试值（建议先 +0.5 V，再 +1.0 V；确认极性和量程正常后再试负值），观察对应 AI 曲线。
3. AO2、AO3 没有接入 AI 回环。AI2、AI3 输入悬空，读数可能漂移、噪声较大或随环境变化，**不能把它们的读数当作 0 V 基准，也不能用它们判断采集程序是否正确**。GUI 应将 AI2/AI3 标记为“未接线/悬空”，而不是判定为零值。
4. 初次通信时所有 AO 命令必须初始化为 0 V；未确认从站处于 OP、PDO 映射正确、WKC 正常之前，不允许自动输出波形。
5. 测试前复核 AO1/AO4 到 AI1/AI4 的端子编号和极性，并用万用表测量 AO 输出；不要在未确认 AO 输出为 0 V 时改接线。
6. 记录当前供电实际值、Us/Up LED、X1 Link/Act、E-bus Link/Act、RUN 状态，以及 SOEM 扫描得到的 Vendor ID、Product Code、PDO byte/bit offset 和 ExpectedWKC。

### 7.5 当前实物状态摘要

| 项目 | 当前记录 |
|---|---|
| 模块顺序 | EK1100 → EL3104 → EL4134 |
| Us（上方 24 V/0 V） | 已接 20 V DC；低于 EK1100 规格下限 20.4 V，测试前建议调至 24.0 V |
| Up（下方 power contacts +/−） | 已接 20 V DC / 0 V；对当前 EL3104、EL4134 工作并非必需 |
| AO1 → AI1 | 已双线连接 |
| AO4 → AI4 | 已双线连接 |
| AO2/AI2 | 未连接 |
| AO3/AI3 | 未连接 |
| X1 网口 | 待连接 Windows USB Ethernet 网卡 |
| X2 网口 | 暂未连接下游设备 |
| EL9011 端盖 | 用户本次未说明，待现场确认 |

