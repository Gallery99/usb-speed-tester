# USB 测速小工具（USB Speed Tester）

一个 400×500 的单窗口小工具：检测 USB 存储设备 / 移动固态 / 雷电设备 / 安卓手机（MTP 传输文件模式），点击后立即执行 **1000MB 写入 + 读回**，实时绿色速度条显示速率，完成后按预设等级表给出设备速度对标。

## 使用方法

1. 双击 `dist/USBSpeedTester.exe`（单文件，约 10MB，无需安装 Python 或任何依赖）。
2. 首页四个象限自动展示检测到的 USB 设备（U 盘、Type-C 移动固态、雷电/USB4 设备）以及打开了「传输文件」模式的安卓手机。
3. 点击任一设备，立即开始测试：
   - 写入 1000MB（带 fsync 强制落盘）→ 读回 1000MB
   - 绿色 VU 条实时跳动显示当前速度（0–1000 MB/s 刻度）
   - 空间不足时自动缩小测试量并在结果页标注；测试中可随时取消
4. 完成自动跳转结果界面：
   - 读 / 写速度（MB/s）
   - 设备接口判定（雷电 / USB4 / Type-C / USB 3.x / USB 2.0，依据 PnP 控制器链 + 实测速度）
   - 「设备速度相当于」等级对标卡（按实测写速度匹配）与完整 8 级对照表
   - 可保存 TXT 测试报告到桌面

## 速度等级表

| 实测写速度 | 设备速度相当于 | 备注 |
|---|---|---|
| ≤15MB/s | 老旧廉价 USB2.0 白牌 U 盘 | 十几块杂牌老 U 盘，写文档还行，大文件极慢 |
| 16~40MB/s | 普通入门 USB3.0（3.2Gen1）常规 U 盘（闪迪 CZ73 / 海康 X302） | 市面销量最大的平价金属 U 盘，读快写慢 |
| 41~100MB/s | 中端 USB3.2 Gen1 高速 U 盘 | 办公拷贝大文件主力款 |
| 101~280MB/s | USB3.2 Gen2 固态 U 盘（雷克沙 M900 入门） | 固态 U 盘起步，比普通 U 盘强很多 |
| 281~500MB/s | 高端 USB3.2 Gen2 固态 U 盘（闪迪至尊超极速） | 高端闪存 U 盘，接近入门 PSSD 移动固态 |
| 501~900MB/s | 入门 Type-C 移动固态硬盘 PSSD（三星 T7 低端档） | 小体积固态，4K 剪辑可用 |
| 900~1400MB/s | 主流 Type-C PSSD（三星 T7/T9、西数 MyPassport 随行 SSD） | 大众热门移动固态，USB3.2Gen2 满速水平 |
| ≥1400MB/s | 雷电 3 / 雷电 4 高速移动固态（三星 T9 顶配、USB4 NVMe 固态） | 专业级高速存储，雷电 4 满速档 |

注：写速度受文件系统与设备固件影响会自然波动；读速度未绕过系统缓存，偏乐观。

## 源码与构建

纯 Python 3 + Tkinter，无第三方依赖（`app.py` 单文件，约 900 行）。

```bash
pip install pyinstaller
pyinstaller --onefile --noconsole --name USBSpeedTester app.py
```

### 实现要点

- **设备检测**：PowerShell 脚本（通过 `$env:USBSD_OUT` 写 UTF-8 结果文件回传，规避 console 编码问题）
  - USB 大容量存储：`Win32_DiskDrive`(InterfaceType=USB) + WMI 关联查询取盘符/卷标/剩余空间
  - 安卓手机 MTP：`Get-PnpDevice -Class WPD` + Shell.Application 便携设备命名空间
- **接口判定**：PnP 祖先链（DEVPKEY_Device_Parent）中的 USB 根集线器/Thunderbolt/USB4 控制器名 + 实测速度联合推断
- **U 盘测速**：Python 线程写 1000MB → fsync → 读回，实时回调更新绿色 VU 条与进度条
- **MTP 测速**：Shell CopyHere 复制 + 轮询 `System.Size`/本地文件长度计算实时速率，完成后清理手机侧测试文件

## 环境要求

- Windows 10/11（依赖 PowerShell + WMI + Shell COM）
- 安卓手机需手动开启「传输文件 / MTP」模式后再连接 USB
