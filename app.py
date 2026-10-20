# -*- coding: utf-8 -*-
"""USB 测速小工具 —— 单文件 Tkinter 应用。

界面：400x500，三个界面（选择设备 / 测试中 / 结果）。
检测范围：USB 大容量存储（U盘、Type-C 移动固态、雷电/USB4 设备）
以及安卓手机 MTP 文件传输模式（打开手机"传输文件"后插 USB）。
检测设备 -> 点击象限 -> 立即写入并读回 1000MB -> 跳转结果界面。
"""
import ctypes
import json
import math
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time

import tkinter as tk
from tkinter import messagebox

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

APP_W, APP_H = 400, 500
TEST_BYTES = 1000 * 1024 * 1024  # 1000 MB
FONT = "Microsoft YaHei"

BG = "#f2f4f7"
CARD = "#ffffff"
GREEN = "#22c55e"
GREEN_D = "#16a34a"
GREEN_L = "#dcfce7"
TEXT = "#111827"
MUTED = "#6b7280"
LINE = "#e5e7eb"

# ---------------------------------------------------------------- 等级表（按用户要求原文复制）
TIER_TABLE = [
    ("≤15MB/s", "老旧廉价 USB2.0 白牌 U 盘", "十几块杂牌老 U 盘，写文档还行，大文件极慢"),
    ("16~40MB/s", "普通入门 USB3.0（3.2Gen1）常规 U 盘（闪迪 CZ73 / 海康 X302）", "市面销量最大的平价金属 U 盘，读快写慢"),
    ("41~100MB/s", "中端 USB3.2 Gen1 高速 U 盘（【C1-C10 U1-U10内存卡，按实际速度评估u等级】）", "办公拷贝大文件主力款"),
    ("101~280MB/s", "USB3.2 Gen2 固态 U 盘（雷克沙 M900 入门，【C1-C10 U1-U10内存卡，按实际速度评估u等级】）", "固态 U 盘起步，比普通 U 盘强很多"),
    ("281~500MB/s", "高端 USB3.2 Gen2 固态 U 盘（闪迪至尊超极速）", "高端闪存 U 盘，接近入门 PSSD 移动固态"),
    ("501~900MB/s", "入门 Type‑C 移动固态硬盘 PSSD（三星 T7 低端档）", "小体积固态，4K 剪辑可用"),
    ("900~1400MB/s", "主流 Type‑C PSSD（三星 T7/T9、西数 MyPassport 随行 SSD）", "大众热门移动固态，USB3.2Gen2 满速水平"),
    ("≥1400MB/s", "雷电 3 / 雷电 4 高速移动固态（三星 T9 顶配、USB4 NVMe 固态）", "专业级高速存储，雷电 4 满速档"),
]


def tier_for_write(w):
    if w <= 15:
        return 0
    if w <= 40:
        return 1
    if w <= 100:
        return 2
    if w <= 280:
        return 3
    if w <= 500:
        return 4
    if w <= 900:
        return 5
    if w <= 1400:
        return 6
    return 7


def human_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return "%.1f%s" % (n, u)
        n /= 1024
    return "?"


# ---------------------------------------------------------------- PowerShell 辅助脚本
PS_DISK = r'''
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'SilentlyContinue'
$rows = @()
Get-CimInstance Win32_DiskDrive | Where-Object { $_.InterfaceType -eq 'USB' } | ForEach-Object {
    $d = $_
    $letters = @()
    $vols = @()
    $parts = Get-CimAssociatedInstance -InputObject $d -ResultClassName Win32_DiskPartition -ErrorAction SilentlyContinue
    foreach ($p in $parts) {
        $logicals = Get-CimAssociatedInstance -InputObject $p -ResultClassName Win32_LogicalDisk -ErrorAction SilentlyContinue
        foreach ($ld in $logicals) {
            if ($ld.DeviceID -match '^([A-Z]):') { $letters += $Matches[1] }
            $vols += ('Label=' + $ld.VolumeName + '|FS=' + $ld.FileSystem + '|Size=' + $ld.Size + '|Free=' + $ld.FreeSpace)
        }
    }
    $rows += [pscustomobject]@{
        Name       = if ($d.FriendlyName) { $d.FriendlyName } else { $d.Model }
        MediaType  = $d.MediaType
        SizeBytes  = [int64]$d.Size
        Letters    = ($letters -join ',')
        PnPDeviceID = $d.PNPDeviceID
        VolumeInfo = ($vols -join ';')
    }
}
$out = @($rows) | ConvertTo-Json -Depth 3 -Compress
if ($env:USBSD_OUT) { Set-Content -Path $env:USBSD_OUT -Value $out -Encoding UTF8 } else { $out }
'''

PS_ANCESTORS = r'''
param([string]$InstanceId)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'SilentlyContinue'
if (-not $InstanceId) { return }
$all = Get-PnpDevice
$names = @()
$cur = $InstanceId
for ($i = 0; $i -lt 6 -and $cur; $i++) {
    $p = Get-PnpDeviceProperty -InstanceId $cur -KeyName 'DEVPKEY_Device_Parent' -ErrorAction SilentlyContinue
    if (-not $p -or -not $p.Data) { break }
    $parentId = if ($p.Data -is [array]) { (-join $p.Data) } else { [string]$p.Data }
    if (-not $parentId) { break }
    $dev = $all | Where-Object { $_.InstanceId -eq $parentId }
    if ($dev) { $names += ($dev.FriendlyName + ' [' + $dev.Class + ']') }
    $cur = $parentId
}
$out = $names -join ' | '
if ($env:USBSD_OUT) { Set-Content -Path $env:USBSD_OUT -Value $out -Encoding UTF8 } else { $out }
'''

PS_WPD = r'''
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'SilentlyContinue'
$all = Get-PnpDevice
$shell = New-Object -ComObject Shell.Application
$pc = $shell.NameSpace(0x11)
$names = $all | Where-Object { $_.Class -eq 'WPD' } | Select-Object -ExpandProperty FriendlyName
$out = @()
$seen = @{}
foreach ($n in $names) {
    if (-not $n) { continue }
    $item = $pc.Items() | Where-Object { -not $_.IsFileSystem -and ($_.Name -like ('*' + $n + '*') -or $n -like ('*' + $_.Name + '*')) } | Select-Object -First 1
    if (-not $item) { continue }
    if ($seen.ContainsKey($item.Path)) { continue }
    $seen[$item.Path] = 1
    $cap = 0
    foreach ($c in $item.Items()) {
        if ($c.IsFolder) { try { $cap += [double]$c.ExtendedProperty('System.Capacity') } catch {} }
    }
    $out += [pscustomobject]@{ Name = $item.Name; Path = $item.Path; Capacity = [int64]$cap }
}
$out = @($out) | ConvertTo-Json -Depth 3 -Compress
if ($env:USBSD_OUT) { Set-Content -Path $env:USBSD_OUT -Value $out -Encoding UTF8 } else { $out }
'''

PS_MTP_TEST = r'''
param(
    [Parameter(Mandatory=$true)][string]$DevicePath,
    [Parameter(Mandatory=$true)][string]$Leaf,
    [Parameter(Mandatory=$true)][double]$SizeMB,
    [Parameter(Mandatory=$true)][string]$TmpDir
)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Stop'
function Emit($o) { Write-Output ('JSON:' + ($o | ConvertTo-Json -Compress -Depth 5)) }

$shell = New-Object -ComObject Shell.Application
$root = $shell.NameSpace($DevicePath)
if (-not $root) { Emit @{ error = 'device-not-ready' }; exit 2 }

$best = $null; $bestCap = -1
foreach ($c in $root.Items()) {
    if ($c.IsFolder -and $c.Name) {
        $cap = 0
        try { $cap = [double]$c.ExtendedProperty('System.Capacity') } catch {}
        if ($cap -gt $bestCap) { $bestCap = $cap; $best = $c }
    }
}
$folder = if ($best) { $shell.NameSpace($best.Path) } else { $root }

$src = Join-Path $TmpDir $Leaf
$srcLen = (Get-Item -LiteralPath $src -ErrorAction Stop).Length
$need = [int64]($SizeMB * 1MB)
if ($need -gt $srcLen) { $need = $srcLen }

# 若手机可用空间不足则按可用空间截断源文件
$freeB = 0
try { $freeB = [uint64]$best.ExtendedProperty('System.FreeSpace') } catch {}
if ($freeB -gt 0) {
    $capB = [int64]($freeB * 0.85)
    if ($capB -lt 32MB) { Emit @{ error = ('no-space:' + [int]($freeB/1MB)) }; exit 4 }
    if ($capB -lt $need) {
        $need = $capB
        $fs = [System.IO.File]::Open($src, 'Open', 'Write')
        $fs.SetLength($need); $fs.Close()
    }
}

$flags = 4 + 16 + 512 + 1024 + 16384
$verdict = @{}

# ---- 写：本地源文件复制到手机
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$folder.CopyHere($src, $flags)
while ($true) {
    Start-Sleep -Milliseconds 250
    $it = $folder.ParseName($Leaf)
    $sz = 0
    if ($it) { try { $sz = [uint64]$it.ExtendedProperty('System.Size') } catch {} }
    if ($sz -gt 0) {
        $mbs = ($sz / 1MB) / [Math]::Max($sw.Elapsed.TotalSeconds, 0.001)
        $frac = [double]$sz / [double]$need
        Write-Output ('PROG:write:%.1f:%.3f' -f $mbs, $frac)
        if ($sz -ge $need) { break }
    } else { Write-Output 'PROG:write:0:0' }
    if ($sw.Elapsed.TotalSeconds -gt 1800) { Emit @{ error = 'write-timeout' }; exit 3 }
}
$verdict.wsec = $sw.Elapsed.TotalSeconds
$verdict.wbytes = $need

# ---- 读：手机文件复制回本地
$readLocal = Join-Path $TmpDir ('from_' + $Leaf)
$sw2 = [System.Diagnostics.Stopwatch]::StartNew()
$dest = $shell.NameSpace($TmpDir)
$dest.CopyHere($it, $flags)
while ($true) {
    Start-Sleep -Milliseconds 250
    if (Test-Path -LiteralPath $readLocal) {
        $l = (Get-Item -LiteralPath $readLocal).Length
        $mbs = ($l / 1MB) / [Math]::Max($sw2.Elapsed.TotalSeconds, 0.001)
        $frac = [double]$l / [double]$need
        Write-Output ('PROG:read:%.1f:%.3f' -f $mbs, $frac)
        if ($l -ge $need) { break }
    } else { Write-Output 'PROG:read:0:0' }
    if ($sw2.Elapsed.TotalSeconds -gt 1800) { Emit @{ error = 'read-timeout' }; exit 3 }
}
$verdict.rsec = $sw2.Elapsed.TotalSeconds
$verdict.rbytes = $need

# ---- 清理手机侧测试文件
try { $it.InvokeVerb('delete') } catch {}
Remove-Item -LiteralPath $readLocal -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $src -Force -ErrorAction SilentlyContinue

Emit @{ ok = $true; wsec = $verdict.wsec; rsec = $verdict.rsec; wbytes = $verdict.wbytes; rbytes = $verdict.rbytes }
'''


_ps_counter = [0]


def write_ps(name, content):
    # utf-8-sig：PowerShell 5.1 无 BOM 会把文件按 ANSI 解析导致中文乱码
    _ps_counter[0] += 1
    path = os.path.join(tempfile.gettempdir(),
                       "_usbspd_%s_%d.ps1" % (name.replace(".ps1", ""), _ps_counter[0]))
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write(content)
    return path


def run_ps_file(ps_path, args=(), timeout=90):
    """执行 PS 脚本；脚本通过 $env:USBSD_OUT 写入 UTF-8 结果文件（绕开 console 编码）。"""
    import uuid
    out = os.path.join(tempfile.gettempdir(), "usbspd_out_%s.txt" % uuid.uuid4().hex[:8])
    env = dict(os.environ)
    env["USBSD_OUT"] = out
    cmd = ["powershell", "-NoProfile", "-NoLogo", "-NonInteractive",
           "-ExecutionPolicy", "Bypass", "-File", ps_path] + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, env=env,
                       creationflags=CREATE_NO_WINDOW)
    data = ""
    try:
        with open(out, "r", encoding="utf-8-sig") as f:
            data = f.read()
    except OSError:
        data = r.stdout or ""
    try:
        os.remove(out)
        os.remove(ps_path)
    except OSError:
        pass
    return data


def run_ps(script, args=(), timeout=90):
    return run_ps_file(write_ps("_usbspd_tmp.ps1", script), args, timeout)


def _parse_disks(out):
    try:
        data = json.loads(out) if out.strip() else []
        if isinstance(data, dict):
            data = [data]
    except ValueError:
        data = []
    disks = []
    for d in data or []:
        letters = (d.get("Letters") or "").strip()
        letter = letters.split(",")[0].strip() if letters else ""
        name = (d.get("Name") or "USB 设备").strip()
        media = (d.get("MediaType") or "")
        vol_label, vol_size, vol_free, fs = "", 0, 0, ""
        for part in (d.get("VolumeInfo") or "").split(";"):
            m = {}
            for kv in part.split("|"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    m[k] = v
            if m:
                vol_label = m.get("Label", "") or ""
                fs = m.get("FS", "")
                try:
                    vol_size = int(float(m.get("Size", 0)))
                    vol_free = int(float(m.get("Free", 0)))
                except ValueError:
                    pass
        kind = "移动固态 / 硬盘" if ("Fixed" in media or "External" in media) else "U 盘 / 存储卡"
        title = vol_label if vol_label else name
        size_bytes = vol_size or int(d.get("SizeBytes") or 0)
        sub = human_size(size_bytes)
        if vol_free:
            sub += " · 可用 " + human_size(vol_free)
        if "nvme" in name.lower():
            kind = "Type-C 固态"
        disks.append({
            "kind": "disk", "letter": letter, "name": title, "model": name,
            "kind_tag": kind, "sub": sub + (" · " + fs if fs else ""),
            "ancestors": "", "pnpid": d.get("PnPDeviceID") or "",
            "size_bytes": size_bytes, "ready": bool(letter),
        })
    return disks


def _parse_mtp(out):
    try:
        data = json.loads(out) if out.strip() else []
        if isinstance(data, dict):
            data = [data]
    except ValueError:
        data = []
    phones, seen = [], set()
    for w in data or []:
        path = w.get("Path") or ""
        if not path or path in seen:
            continue
        seen.add(path)
        cap = int(w.get("Capacity") or 0)
        phones.append({
            "kind": "mtp", "path": path, "name": w.get("Name") or "安卓手机",
            "kind_tag": "安卓手机 MTP", "sub": (human_size(cap) if cap else "传输文件模式") + " · MTP",
            "ancestors": "", "size_bytes": cap, "ready": True,
        })
    return phones


def _dbg(msg):
    try:
        with open(os.path.join(tempfile.gettempdir(), "usbspd_debug.log"), "a",
                  encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def detect_devices():
    """返回设备列表；每项 kind=disk(带盘符) 或 mtp(手机)。"""
    result = {"disks": [], "phones": []}
    t0 = time.time()
    result["disks"] = _parse_disks(run_ps(PS_DISK))
    _dbg("PS_DISK %.1fs disks=%d" % (time.time() - t0, len(result["disks"])))
    t0 = time.time()
    result["phones"] = _parse_mtp(run_ps(PS_WPD))
    _dbg("PS_WPD %.1fs phones=%d" % (time.time() - t0, len(result["phones"])))
    return result["disks"] + result["phones"]


# ---------------------------------------------------------------- 速度测试
def free_bytes(path):
    if not IS_WINDOWS:
        return None
    free = ctypes.c_ulonglong(0)
    ok = ctypes.windll.kernel32.GetDiskFreeSpaceExW(
        ctypes.c_wchar_p(path), None, None, ctypes.byref(free))
    return free.value if ok else None


def disk_test(letter, on_tick, stop_event):
    """写入并读回 1000MB，返回 {'write','read','bytes','truncated'} 或 None。"""
    root = letter + ":\\"
    size = TEST_BYTES
    truncated = False
    try:
        free = free_bytes(root)
        if free and free < size + 64 * 1024 * 1024:
            size = max(64 * 1024 * 1024, int(free * 0.75))
            truncated = True
    except Exception:
        pass
    path = os.path.join(root, "_usbspd_%d.bin" % int(time.time()))
    buf = b"\0" * (4 * 1024 * 1024)
    CHUNK = 0.15

    # 写
    t0 = time.perf_counter(); written = 0; last = t0; last_w = 0
    on_tick("write", 0.0, 0.0)
    with open(path, "wb") as f:
        while written < size and not stop_event.is_set():
            f.write(buf)
            written += len(buf)
            now = time.perf_counter()
            if now - last >= CHUNK:
                on_tick("write", written / size, (written - last_w) / (now - last))
                last, last_w = now, written
        f.flush()
        os.fsync(f.fileno())
    w_sec = time.perf_counter() - t0
    on_tick("write", 1.0, 0.0)

    # 读
    t0 = time.perf_counter(); readn = 0; last = t0; last_r = 0
    on_tick("read", 0.0, 0.0)
    with open(path, "rb") as f:
        while readn < size and not stop_event.is_set():
            chunk = f.read(4 * 1024 * 1024)
            if not chunk:
                break
            readn += len(chunk)
            now = time.perf_counter()
            if now - last >= CHUNK:
                on_tick("read", readn / size, (readn - last_r) / (now - last))
                last, last_r = now, readn
    r_sec = time.perf_counter() - t0
    on_tick("read", 1.0, 0.0)

    try:
        os.remove(path)
    except OSError:
        pass
    if stop_event.is_set():
        return None
    return {
        "write": (size / w_sec) / (1024 * 1024),
        "read": (readn / r_sec) / (1024 * 1024),
        "bytes": size, "truncated": truncated,
    }


def mtp_test(device_path, on_tick, stop_event):
    """安卓 MTP：远端复制 1000MB。"""
    tmpdir = tempfile.mkdtemp(prefix="usbspd_")
    leaf = "_usbspd_%d.bin" % int(time.time() * 1000)
    src = os.path.join(tmpdir, leaf)
    try:
        with open(src, "wb") as f:
            f.truncate(TEST_BYTES)
        script = write_ps("_usbspd_mtp.ps1", PS_MTP_TEST)
        cmd = ["powershell", "-NoProfile", "-NoLogo", "-NonInteractive",
               "-ExecutionPolicy", "Bypass", "-File", script,
               "-DevicePath", device_path, "-Leaf", leaf,
               "-SizeMB", "%d" % (TEST_BYTES // (1024 * 1024)), "-TmpDir", tmpdir]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                creationflags=CREATE_NO_WINDOW)
        result = None
        on_tick("write", 0.0, 0.0)
        for line in proc.stdout:
            if stop_event.is_set():
                try:
                    proc.kill()
                except Exception:
                    pass
                return None
            line = line.strip()
            if not line:
                continue
            if line.startswith("PROG:"):
                try:
                    _, phase, mbs, frac = line.split(":")
                    on_tick(phase, float(frac), float(mbs) * 1024 * 1024)
                except ValueError:
                    pass
            elif line.startswith("JSON:"):
                try:
                    result = json.loads(line[5:])
                except ValueError:
                    pass
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        if stop_event.is_set():
            return None
        if not result or not result.get("ok"):
            err = (result or {}).get("error", "unknown")
            if err == "device-not-ready":
                raise RuntimeError("手机未响应：请确认已选择\"传输文件\"模式并重新插拔")
            if err == "no-space":
                raise RuntimeError("手机可用空间不足，请清理后再试")
            if "timeout" in err:
                raise RuntimeError("传输超时，请检查数据线或重新插拔手机")
            raise RuntimeError("MTP 测试失败：" + str(err))
        return {
            "write": (result["wbytes"] / (1024 * 1024)) / max(result["wsec"], 0.001),
            "read": (result["rbytes"] / (1024 * 1024)) / max(result["rsec"], 0.001),
            "bytes": result["wbytes"], "truncated": result["wbytes"] < TEST_BYTES,
        }
    finally:
        try:
            for n in os.listdir(tmpdir):
                os.remove(os.path.join(tmpdir, n))
            os.rmdir(tmpdir)
        except OSError:
            pass


# ---------------------------------------------------------------- 接口判定
def interface_label(name, ancestors, write, read):
    a = (ancestors or "")
    la = a.lower()
    best = max(write or 0, read or 0)
    nm = (name or "").lower()
    if "thunderbolt" in la or "雷电" in a:
        return "雷电 3 / 雷电 4（Thunderbolt）"
    if "usb4" in la:
        return "USB4 高速接口"
    hw = None
    if ("usb 3" in la or "usb3" in la or "superspeed" in la or "xhci" in la
            or "usb 3.1" in la or "usb 3.2" in la):
        hw = "3"
    elif "usb 2" in la or "usb2" in la or "ehci" in la or "uhci" in la or "ohci" in la:
        hw = "2"
    if "nvme" in nm and best > 400:
        return "Type-C（USB 3.2 Gen2 NVMe 固态）"
    if hw == "2":
        return "USB 2.0（480 Mbps 端口）"
    if hw == "3":
        if best <= 55:
            return "USB 3.x 设备（当前插在 USB 2.0 端口）"
        if best > 450:
            return "Type-C（USB 3.2 Gen2 / USB4）"
        return "USB 3.x（SuperSpeed）"
    if best <= 45:
        return "USB 2.0（480 Mbps，按实测推断）"
    if best <= 450:
        return "USB 3.x（SuperSpeed，按实测推断）"
    return "Type-C / 雷电（按实测推断）"


class SpeedApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("USB 测速小工具")
        self.geometry("%dx%d" % (APP_W, APP_H))
        self.resizable(False, False)
        self.configure(bg=BG)
        self._center()
        self.container = tk.Frame(self, bg=BG)
        self.container.pack(fill="both", expand=True)
        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.table_open = False
        self._screen = "sel"
        self.after(80, self._poll_queue)
        self.show_selection()

    def _center(self):
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry("+%d+%d" % ((sw - APP_W) // 2, max((sh - APP_H) // 2 - 40, 0)))

    def _clear(self):
        for w in self.container.winfo_children():
            w.destroy()

    # ------------------------------------------------ 界面一：选择设备
    def show_selection(self):
        self._clear()
        self.table_open = False
        self._screen = "sel"
        tk.Label(self.container, text="选择 USB 设备", font=(FONT, 24, "bold"),
                 bg=BG, fg=TEXT).pack(pady=(28, 4))
        tk.Label(self.container, text="点击任意设备，立即开始 1000MB 读+写测试",
                 font=(FONT, 9), bg=BG, fg=MUTED).pack()
        self.grid_frame = tk.Frame(self.container, bg=BG)
        self.grid_frame.pack(pady=(18, 0))
        self._quadrants = []
        for i in range(4):
            r, c = divmod(i, 2)
            b = tk.Label(self.grid_frame, text="…", width=19, height=6,
                         bg=CARD, fg=MUTED, font=(FONT, 10),
                         highlightbackground=LINE, highlightthickness=1,
                         cursor="hand2")
            b.grid(row=r, column=c, padx=6, pady=6)
            b.bind("<Button-1>", lambda e, idx=i: self._on_click(idx))
            self._quadrants.append(b)
        self.status = tk.Label(self.container, text="正在扫描设备…",
                               font=(FONT, 9), bg=BG, fg=MUTED)
        self.status.pack(pady=(14, 2))
        tk.Button(self.container, text="重新扫描", font=(FONT, 9), command=self._rescan,
                  relief="flat", bg="#e5e7eb", fg=TEXT, cursor="hand2").pack()
        tk.Label(self.container,
                 text="支持 U盘 / Type-C 移动固态 / 雷电设备 / 安卓手机 MTP 传输模式",
                 font=(FONT, 7), bg=BG, fg="#9ca3af").pack(side="bottom", pady=6)
        self.devices = []
        self._rescan()

    def _rescan(self):
        self.status.config(text="正在扫描设备…（首次约需几秒）")
        for b in self._quadrants:
            b.config(text="…", fg=MUTED)
        threading.Thread(target=self._detect_worker, daemon=True).start()

    def _detect_worker(self):
        out = {}
        errors = []

        def run(key, fn):
            try:
                out[key] = fn()
            except Exception as e:
                _dbg("detect %s failed: %r" % (key, e))
                errors.append(str(e))

        t1 = threading.Thread(target=run, args=("disks", lambda: _parse_disks(run_ps(PS_DISK))))
        t2 = threading.Thread(target=run, args=("phones", lambda: _parse_mtp(run_ps(PS_WPD))))
        _dbg("detect start")
        t1.start(); t2.start()
        t1.join(); t2.join()
        _dbg("detect done disks=%s phones=%s errors=%s" %
             (len(out.get("disks", [])), len(out.get("phones", [])), errors))
        if errors and not out:
            self.q.put(("status", "扫描失败：" + errors[0]))
            self.q.put(("devices", []))
            return
        # 分阶段回报：存储设备先上屏，手机随后
        if "disks" in out:
            self.q.put(("devices", out["disks"]))
            time.sleep(0.05)
        if "phones" in out:
            self.q.put(("devices", out.get("disks", []) + out.get("phones", [])))
        self.devices = out.get("disks", []) + out.get("phones", [])

    def _on_click(self, idx):
        if idx >= len(self.devices):
            return
        dev = self.devices[idx]
        if dev["kind"] == "disk" and not dev.get("ready"):
            messagebox.showwarning("无法测试", "该设备没有可用盘符")
            return
        self.show_testing(dev)

    # ------------------------------------------------ 界面二：测试中
    def show_testing(self, dev):
        self._clear()
        self._screen = "test"
        self.stop_event.clear()
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        self.cur_dev = dev
        self.ticks = []
        tk.Label(self.container, text=dev["name"], font=(FONT, 13, "bold"),
                 bg=BG, fg=TEXT).pack(pady=(24, 2))
        self.phase_lab = tk.Label(self.container, text="正在写入 …", font=(FONT, 10),
                                  bg=BG, fg=MUTED)
        self.phase_lab.pack()
        self.speed_lab = tk.Label(self.container, text="0", font=(FONT, 30, "bold"),
                                  bg=BG, fg=GREEN_D)
        self.speed_lab.pack(pady=(10, 0))
        tk.Label(self.container, text="MB/s", font=(FONT, 9), bg=BG,
                 fg=MUTED).pack()
        self.vu = tk.Canvas(self.container, width=350, height=26, bg="#e5e7eb",
                            highlightthickness=0)
        self.vu.pack(pady=(18, 0))
        for x, lab in ((0, "0"), (73, "250"), (146, "500"), (219, "750"), (292, "1000")):
            self.vu.create_line(x, 26, x + 58, 26, fill="#d1d5db")
            self.vu.create_text(x + 28, 18, text=lab, font=(FONT, 6), fill="#9ca3af")
        self.vu_bar = self.vu.create_rectangle(0, 0, 0, 26, fill=GREEN, width=0)
        self.pbar = tk.Canvas(self.container, width=350, height=12, bg="#e5e7eb",
                              highlightthickness=0)
        self.pbar.pack(pady=(10, 0))
        self.pbar_bar = self.pbar.create_rectangle(0, 0, 0, 12, fill=GREEN, width=0)
        self.avg_lab = tk.Label(self.container, text="平均 0 MB/s · 已完成 0%",
                                font=(FONT, 8), bg=BG, fg=MUTED)
        self.avg_lab.pack(pady=(10, 0))
        tk.Button(self.container, text="取消测试", font=(FONT, 9),
                  command=self._cancel, relief="flat", bg="#e5e7eb",
                  fg=TEXT, cursor="hand2").pack(side="bottom", pady=14)
        threading.Thread(target=self._test_worker, args=(dev,), daemon=True).start()
        # 后台预取控制器链（用于接口判定），测试结束时通常已完成
        if dev.get("pnpid"):
            def prefetch():
                try:
                    dev["ancestors"] = run_ps(PS_ANCESTORS, args=(dev["pnpid"],)).strip()
                except Exception:
                    pass
            threading.Thread(target=prefetch, daemon=True).start()

    def _test_worker(self, dev):
        def on_tick(phase, frac, inst):
            self.q.put(("tick", phase, frac, inst))
        try:
            t0 = time.time()
            if dev["kind"] == "disk":
                res = disk_test(dev["letter"], on_tick, self.stop_event)
            else:
                res = mtp_test(dev["path"], on_tick, self.stop_event)
            if res:
                res["device"] = dev
                res["elapsed"] = time.time() - t0
            self.q.put(("result", res))
        except Exception as e:
            self.q.put(("result", {"error": str(e), "device": dev}))

    def _finish(self, res):
        if res is None:
            self.show_selection()
            return
        if res.get("error"):
            messagebox.showerror("测试失败", res.get("error"))
            self.show_selection()
            return
        self.show_result(res)

    # ------------------------------------------------ 界面三：结果
    def show_result(self, res):
        self._clear()
        self._screen = "result"
        self.table_win = None
        dev = res["device"]
        w, r = res["write"], res["read"]
        iface = interface_label(dev.get("model") or dev["name"], dev.get("ancestors", ""), w, r)
        self.cur_tier = tier_for_write(w)
        tier = TIER_TABLE[self.cur_tier]
        tk.Label(self.container, text="测试完成", font=(FONT, 20, "bold"),
                 bg=BG, fg=TEXT).pack(pady=(20, 2))
        tk.Label(self.container, text=dev["name"], font=(FONT, 9), bg=BG,
                 fg=MUTED).pack()

        row = tk.Frame(self.container, bg=CARD, highlightbackground=LINE,
                       highlightthickness=1)
        row.pack(fill="x", padx=16, pady=(12, 0))
        for i, (lab, val) in enumerate((("写入", "%.0f" % w), ("读取", "%.0f" % r))):
            f = tk.Frame(row, bg=CARD)
            f.grid(row=0, column=i, padx=(34, 34), pady=(10, 8))
            tk.Label(f, text=lab + " MB/s", font=(FONT, 8), bg=CARD, fg=MUTED).pack()
            tk.Label(f, text=val, font=(FONT, 26, "bold"), bg=CARD,
                     fg=GREEN_D).pack()

        card = tk.Frame(self.container, bg=CARD, highlightbackground=LINE,
                        highlightthickness=1)
        card.pack(fill="x", padx=16, pady=(10, 0))
        tk.Label(card, text="设备接口：" + iface, font=(FONT, 9), bg=CARD,
                 fg=TEXT, wraplength=330, justify="left").pack(anchor="w", padx=12, pady=(10, 4))
        note = dev["kind_tag"] + " · 测试量 " + human_size(res["bytes"])
        if res.get("truncated"):
            note += "（空间不足已缩减）"
        tk.Label(card, text=note, font=(FONT, 8), bg=CARD, fg=MUTED,
                 anchor="w").pack(anchor="w", padx=12, pady=(0, 10))

        tc = tk.Frame(self.container, bg=CARD, highlightbackground=GREEN,
                      highlightthickness=1)
        tc.pack(fill="x", padx=16, pady=(10, 0))
        tk.Label(tc, text="设备速度相当于", font=(FONT, 9, "bold"), bg=CARD,
                 fg=GREEN_D).pack(anchor="w", padx=12, pady=(8, 2))
        tk.Label(tc, text="实测写速度 " + tier[0], font=(FONT, 8), bg=CARD,
                 fg=MUTED, anchor="w").pack(anchor="w", padx=12)
        msg = tk.Message(tc, text=tier[1], font=(FONT, 10, "bold"), bg=CARD,
                         fg=TEXT, width=330, justify="left")
        msg.pack(anchor="w", padx=12, pady=(4, 0))
        tk.Label(tc, text=tier[2], font=(FONT, 8), bg=CARD, fg=MUTED,
                 wraplength=330, justify="left").pack(anchor="w", padx=12, pady=(4, 10))

        self.table_btn = tk.Button(self.container, text="查看完整对照表 ▾", font=(FONT, 8),
                                   relief="flat", bg="#e5e7eb", fg=TEXT, cursor="hand2",
                                   command=self._toggle_table)
        self.table_btn.pack(pady=(10, 0))
        self.table_frame = None

        btns = tk.Frame(self.container, bg=BG)
        btns.pack(side="bottom", pady=12)
        tk.Button(btns, text="重新测试", font=(FONT, 9), width=10,
                  command=self.show_selection, relief="flat", bg=GREEN,
                  fg="#ffffff", cursor="hand2").grid(row=0, column=0, padx=8)
        tk.Button(btns, text="保存报告", font=(FONT, 9), width=10,
                  command=lambda: self._save_report(res, iface, tier), relief="flat",
                  bg="#e5e7eb", fg=TEXT, cursor="hand2").grid(row=0, column=1, padx=8)

    def _toggle_table(self):
        if getattr(self, "table_win", None):
            try:
                self.table_win.destroy()
            except Exception:
                pass
            self.table_win = None
            self.table_btn.config(text="查看完整对照表 ▾")
            return
        self.table_btn.config(text="收起对照表 ▴")
        win = tk.Toplevel(self)
        self.table_win = win
        win.title("设备速度等级对照表")
        win.geometry("400x360")
        win.resizable(False, False)
        win.configure(bg=CARD)
        canvas = tk.Canvas(win, bg=CARD, highlightthickness=0)
        scroll = tk.Scrollbar(win, command=canvas.yview)
        inner = tk.Frame(canvas, bg=CARD)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        canvas.bind("<MouseWheel>", lambda e: canvas.yview_scroll(-e.delta // 120, "units"))
        inner.bind("<MouseWheel>", lambda e: canvas.yview_scroll(-e.delta // 120, "units"))
        title = tk.Label(inner, text="实测写速度 → 设备速度相当于", font=(FONT, 10, "bold"),
                         bg=CARD, fg=TEXT)
        title.pack(anchor="w", padx=10, pady=(10, 4))
        for i, (rng, txt, remark) in enumerate(TIER_TABLE):
            f = tk.Frame(inner, bg=GREEN_L if i == self.cur_tier else CARD)
            f.pack(fill="x", padx=8, pady=3)
            tk.Label(f, text=rng, font=(FONT, 8, "bold"),
                     bg=GREEN_L if i == self.cur_tier else CARD, fg=GREEN_D,
                     anchor="w").pack(fill="x", padx=6, pady=(4, 0))
            tk.Label(f, text=txt, font=(FONT, 8), wraplength=340, justify="left",
                     bg=GREEN_L if i == self.cur_tier else CARD, fg=TEXT,
                     anchor="w").pack(fill="x", padx=6)
            tk.Label(f, text=remark, font=(FONT, 7), wraplength=340, justify="left",
                     bg=GREEN_L if i == self.cur_tier else CARD, fg=MUTED,
                     anchor="w").pack(fill="x", padx=6, pady=(0, 4))

    def _save_report(self, res, iface, tier):
        lines = [
            "USB 测速报告",
            "设备：%s（%s）" % (res["device"]["name"], res["device"]["kind_tag"]),
            "接口：%s" % iface,
            "写入：%.1f MB/s" % res["write"],
            "读取：%.1f MB/s" % res["read"],
            "测试量：%s%s" % (human_size(res["bytes"]),
                             "（空间不足已缩减）" if res.get("truncated") else ""),
            "",
            "实测写速度 %s" % tier[0],
            tier[1],
            tier[2],
        ]
        fn = "USBSpeedReport_%s.txt" % time.strftime("%Y%m%d_%H%M%S")
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        path = os.path.join(desktop if os.path.isdir(desktop) else os.path.expanduser("~"), fn)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            messagebox.showinfo("已保存", "报告已保存到桌面：\n" + fn)
        except Exception as e:
            messagebox.showerror("保存失败", str(e))


    def _cancel(self):
        self.stop_event.set()
        self.phase_lab.config(text="已取消")

    def _poll_queue(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "devices" and self._screen == "sel":
                    self._fill_grid(msg[1])
                elif kind == "status" and self._screen == "sel":
                    self.status.config(text=msg[1])
                elif kind == "tick" and self._screen == "test":
                    self._tick(msg[1], msg[2], msg[3])
                elif kind == "result" and self._screen == "test":
                    self._finish(msg[1])
        except queue.Empty:
            pass
        self.after(60, self._poll_queue)

    def _fill_grid(self, devs):
        for i, b in enumerate(self._quadrants):
            if i < len(devs):
                d = devs[i]
                tag = "手机" if d["kind"] == "mtp" else "存储"
                b.config(text="【%s】%s\n\n%s\n\n%s" % (tag, d["name"], d["kind_tag"], d["sub"]),
                         font=(FONT, 9), fg=TEXT, justify="center",
                         highlightbackground=GREEN if d["ready"] else "#d1d5db")
            else:
                b.config(text="未检测到设备", font=(FONT, 9), fg="#9ca3af")
        if devs:
            self.status.config(text="检测到 %d 个 USB 设备，点击开始测速" % len(devs))
        else:
            self.status.config(text="未检测到 USB 设备，请插入 U 盘或连接手机")

    def _tick(self, phase, frac, inst):
        mbs = inst / (1024 * 1024)
        self.speed_lab.config(text="%.0f" % mbs)
        w = 350 * min(mbs / 1000.0, 1.0)
        self.vu.coords(self.vu_bar, 0, 0, w, 26)
        self.pbar.coords(self.pbar_bar, 0, 0, 350 * min(max(frac, 0), 1), 12)
        pct = int(frac * 100)
        self.avg_lab.config(text="%s · 已完成 %d%%" % (
            "正在读取手机数据…" if phase == "read" else "正在写入数据…", pct))
        self.phase_lab.config(text="正在读取 1000MB …" if phase == "read" else "正在写入 1000MB …")


def main():
    app = SpeedApp()
    app.mainloop()


if __name__ == "__main__":
    main()

