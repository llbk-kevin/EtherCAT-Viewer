# 六、开发、打包与发布

## 一、开发和测试

使用 Windows 与 Python 3.12 x64。在项目目录执行以下命令。

```powershell
python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: -r requirements.txt pytest
.\.venv\Scripts\python.exe -X utf8 -m pytest -q
```

测试覆盖量程换算、PDO 结构、波形和周期校验、进程保护、网卡选择与控制台启动路径。测试实机前确认输出安全、匹配端子及设备身份。

独立长测会占用网卡。先从仪表盘断开实机，再查询网卡 GUID 并将示例值替换为目标接口。默认执行 180 秒，结果写入项目 `logs/zero-soak.json`。

```powershell
Get-NetAdapter -Physical | Format-Table Name, Status, LinkSpeed, InterfaceGuid
.\.venv\Scripts\python.exe -X utf8 -m viewer.soak --adapter-guid '{00000000-0000-0000-0000-000000000000}' --seconds 180
```

## 二、构建 Windows x64 单文件

```powershell
python.exe -X utf8 .\build-windows.py
```

脚本使用单独构建虚拟环境、运行时 wheel 和 PyInstaller 6.22.3。生成 `dist/EtherCAT-Viewer-v0.1.0-windows-x64.exe` 及其 `.sha256` 校验文件。构建配方打包本机 Python 运行时、PySOEM 扩展、Web 服务、网页和第三方许可。Npcap 由用户另行安装。使用 `multiprocessing.freeze_support()` 分流 Windows 子进程入口。只读资源从解包目录加载，运行数据存入 `%LOCALAPPDATA%\EtherCAT-Viewer`。

发布文件未做 Authenticode 签名，可比对 Release 的 SHA-256。第三方许可和 SOEM 对应源码见 [第三方组件与许可](../THIRD-PARTY-NOTICES.md)。[PyInstaller 多进程与单文件说明](https://pyinstaller.org/en/latest/common-issues-and-pitfalls.html)

## 三、发布前验收

1. 把 EXE 单独放进含中文和空格的目录，测试可见控制台、默认浏览器打开、八图、网卡选择、进程退出和端口竞争。
2. 以零输出连接实机，验证 OP 和 WKC，记录 10 ms 档三分钟统计。做小幅回环、CSV、清零确认、Ctrl+C 和关窗。
3. 填写 [现场现状与验收记录](05-hardware-acceptance.md)，列出测试环境和未验证项。在受控目录生成校验文件，确认仓库差异与产物均来自拟发布的同一版本。

## 四、GitHub Release

确认可写仓库、分支与版本标签。在 `main` 提交后，建立指向该提交的 `v0.1.0` 注解标签，推送分支和标签。用本地 GitHub CLI 上传 EXE 和 SHA-256，发行说明用中文。可先建草稿核对资产，再正式发布。

```powershell
git tag -a v0.1.0 -m "EtherCAT Viewer v0.1.0"
git push origin main refs/tags/v0.1.0
gh release create v0.1.0 --verify-tag --draft --title "EtherCAT Viewer v0.1.0" --notes-file <发行说明文件> <EXE路径> <校验文件路径>
gh release edit v0.1.0 --draft=false --latest
gh release view v0.1.0 --json url,tagName,isDraft,isPrerelease,assets
```

命令中的尖括号占位符替换为实际路径，含空格的路径加引号。发布后下载两项资产，在本地比对 `Get-FileHash -Algorithm SHA256`，并核对标签、默认分支和 [Releases](https://github.com/llbk-kevin/EtherCAT-Viewer/releases)。[GitHub CLI 发布命令](https://cli.github.com/manual/gh_release_create)
