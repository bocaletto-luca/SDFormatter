# sdFormatter.py
# Professional SD card formatter for Windows (single-file, Python 3.10+)
#
# Features:
# - Safe disk enumeration with rich details (number, size, bus, unique id, letters, partitions)
# - Guided, resilient formatting pipeline (MBR default, single primary partition, FAT32 ≤ 32 GB, exFAT > 32 GB)
# - Optional wipe: metadata-only (default) or full zero-fill (diskpart clean all)
# - Quick (default) or full (surface check) format
# - Label sanitization (camera-friendly option for 8.3-like uppercase labels)
# - Cluster size: system default, AUTO suggestion, or explicit value
# - Post-format verification (filesystem + label), optional I/O test
# - Auto-elevation (UAC), dry-run, JSON report, robust logging
#
# Usage examples:
#   python sdFormatter.py --list
#   python sdFormatter.py --disk 3 --label SDCARD --fs AUTO --yes
#   python sdFormatter.py --disk 3 --label GOPRO --full --cluster AUTO --camera-compat --yes
#   python sdFormatter.py --disk 3 --label MEDIA --fs exFAT --cluster 262144 --test-io --report report.json --yes
#
# Important:
# - Windows only. Requires administrative privileges for formatting.
# - Formatting erases all data on the selected disk. Double-check the disk number.
# - Use --dry-run to preview actions and PowerShell commands without changing anything.

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import re
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional, Tuple

APP_NAME = "SDFormatterPro"
APP_VERSION = "1.0.0"
POWERSHELL = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"]

# ---------------------------
# Logging and elevation
# ---------------------------

def get_log_path() -> str:
    base = os.getenv("LOCALAPPDATA") or os.getcwd()
    log_dir = os.path.join(base, APP_NAME)
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, "app.log")

def setup_logger(verbosity: int) -> logging.Logger:
    logger = logging.getLogger(APP_NAME)
    logger.setLevel(logging.DEBUG)
    # Avoid duplicate handlers if main() runs twice (e.g., after elevation)
    if not logger.handlers:
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.DEBUG if verbosity > 0 else logging.INFO)
        ch.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(ch)

        fh = RotatingFileHandler(get_log_path(), maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(fh)
    else:
        # Update console level if needed
        for h in logger.handlers:
            if isinstance(h, logging.StreamHandler):
                h.setLevel(logging.DEBUG if verbosity > 0 else logging.INFO)
    return logger

def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False

def relaunch_as_admin(logger: logging.Logger, no_elevate: bool) -> None:
    if is_admin() or no_elevate:
        return
    logger.info("Administrative privileges required: requesting elevation (UAC)...")
    try:
        params = subprocess.list2cmdline(sys.argv[1:])
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, f'"{os.path.abspath(sys.argv[0])}" {params}', None, 1
        )
        if ret <= 32:
            raise RuntimeError("Elevation denied or failed")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Auto-elevation failed: {e}")
        logger.error("Please re-run from an Administrator prompt, or use --no-elevate to continue without elevation.")
        sys.exit(2)

# ---------------------------
# PowerShell helpers
# ---------------------------

class PSError(RuntimeError):
    pass

def run_ps(script: str, logger: logging.Logger, timeout: Optional[int] = None, dry_run: bool = False) -> subprocess.CompletedProcess:
    cmd = POWERSHELL + [script]
    logger.debug(f"[PS] {script.strip()}")
    if dry_run:
        class Dummy:
            returncode = 0
            stdout = ""
            stderr = ""
        return Dummy()  # type: ignore
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if p.returncode != 0:
        stderr = (p.stderr or "").strip()
        logger.debug(f"[PS:stderr] {stderr}")
        raise PSError(stderr or "PowerShell error")
    if p.stdout:
        logger.debug(f"[PS:stdout] {p.stdout.strip()}")
    return p

def ps_json(script: str, logger: logging.Logger, dry_run: bool = False) -> Any:
    s = f"({script}) | ConvertTo-Json -Depth 6 -Compress"
    p = run_ps(s, logger=logger, dry_run=dry_run)
    text = (p.stdout or "").strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        cleaned = text.strip("\ufeff \n\r\t")
        return json.loads(cleaned) if cleaned else []

# ---------------------------
# Disk enumeration
# ---------------------------

def list_disks(logger: logging.Logger, dry_run: bool = False) -> List[Dict[str, Any]]:
    discs = ps_json(
        "Get-Disk | Select-Object Number,Size,BusType,FriendlyName,IsSystem,IsBoot,IsReadOnly,IsOffline,UniqueId,PartitionStyle",
        logger, dry_run=dry_run
    )
    if isinstance(discs, dict):
        discs = [discs]
    results: List[Dict[str, Any]] = []
    for d in discs:
        number = int(d["Number"])
        parts = ps_json(
            f"Get-Partition -DiskNumber {number} -ErrorAction SilentlyContinue | "
            "Select-Object DiskNumber,PartitionNumber,DriveLetter,Size,Type,GptType",
            logger, dry_run=dry_run
        )
        if isinstance(parts, dict):
            parts = [parts]
        letters: List[str] = []
        if parts:
            vol_letters = ps_json(
                f"(Get-Partition -DiskNumber {number} -ErrorAction SilentlyContinue | "
                "Get-Volume -ErrorAction SilentlyContinue | Select-Object DriveLetter)",
                logger, dry_run=dry_run
            )
            if isinstance(vol_letters, dict):
                vol_letters = [vol_letters]
            for v in vol_letters or []:
                dl = str(v.get("DriveLetter") or "").strip()
                if dl:
                    letters.append(dl)
        results.append({
            "number": number,
            "size": int(d.get("Size") or 0),
            "bus_type": str(d.get("BusType") or ""),
            "friendly_name": str(d.get("FriendlyName") or ""),
            "is_system": bool(d.get("IsSystem", False)),
            "is_boot": bool(d.get("IsBoot", False)),
            "is_readonly": bool(d.get("IsReadOnly", False)),
            "is_offline": bool(d.get("IsOffline", False)),
            "unique_id": str(d.get("UniqueId") or ""),
            "partition_style": str(d.get("PartitionStyle") or ""),
            "letters": letters,
            "partitions": parts or [],
        })
    return results

def format_size(bytes_size: int) -> str:
    gb = bytes_size / (1024**3)
    if gb >= 1:
        return f"{gb:.1f} GB"
    mb = bytes_size / (1024**2)
    return f"{mb:.0f} MB"

def print_disk_table(disks: List[Dict[str, Any]]) -> None:
    if not disks:
        print("No disks found.")
        return
    headers = ["#", "Size", "Bus", "SYS", "BOOT", "RO", "OFF", "Name", "Letters"]
    widths = [4, 10, 8, 4, 5, 3, 3, 22, 10]
    def fmt(cols: List[str]) -> str:
        return " ".join(str(c).ljust(w) for c, w in zip(cols, widths))
    print(fmt(headers))
    print("-" * (sum(widths) + (len(widths) - 1)))
    for d in disks:
        row = [
            str(d["number"]),
            format_size(d["size"]),
            d["bus_type"],
            "Y" if d["is_system"] else "",
            "Y" if d["is_boot"] else "",
            "Y" if d["is_readonly"] else "",
            "Y" if d["is_offline"] else "",
            (d["friendly_name"] or "")[:22],
            ",".join(d["letters"]) if d["letters"] else "-",
        ]
        print(fmt(row))

# ---------------------------
# Filesystem and cluster policy
# ---------------------------

def suggest_fs(bytes_size: int) -> str:
    gb = bytes_size / (1024**3)
    return "FAT32" if gb <= 32 else "exFAT"

def suggest_cluster(fs: str, bytes_size: int) -> int:
    gb = bytes_size / (1024**3)
    if fs.upper() == "FAT32":
        if gb <= 4:
            return 4096
        elif gb <= 8:
            return 8192
        elif gb <= 16:
            return 16384
        else:
            return 32768
    # exFAT defaults
    if gb <= 64:
        return 131072       # 128 KiB
    elif gb <= 256:
        return 262144       # 256 KiB
    else:
        return 524288       # 512 KiB

# ---------------------------
# Label sanitization
# ---------------------------

INVALID_LABEL_CHARS = r'<>:"/\\|?*'

def sanitize_label(label: str, fs: str, camera_compat: bool) -> str:
    # Enforce basic constraints: remove quotes and invalid characters to avoid PS quoting issues and Windows restrictions.
    raw = (label or "").strip()
    raw = raw.replace("'", "").replace('"', "")
    if camera_compat:
        # Uppercase, A-Z 0-9 space underscore, max 11 (FAT-style)
        base = re.sub(r"[^A-Z0-9 _]", "_", raw.upper())
        base = re.sub(r"\s+", " ", base).strip()
        base = base[:11]
        return base or "SD_CARD"
    # General Windows policy: printable ASCII excluding reserved chars, trim trailing dots/spaces, max 32
    base = "".join(c for c in raw if (32 <= ord(c) < 127) and c not in INVALID_LABEL_CHARS)
    base = base.strip(". ")[:32]
    return base or "SD_CARD"

# ---------------------------
# Formatting pipeline
# ---------------------------

def dismount_volumes(disk_number: int, logger: logging.Logger, dry_run: bool = False) -> None:
    script = f"""
$parts = Get-Partition -DiskNumber {disk_number} -ErrorAction SilentlyContinue
if ($parts) {{
  foreach ($p in $parts) {{
    try {{
      $vol = $p | Get-Volume -ErrorAction SilentlyContinue
      if ($vol -and $vol.DriveLetter) {{
        Dismount-Volume -DriveLetter $vol.DriveLetter -Force -ErrorAction SilentlyContinue | Out-Null
      }}
    }} catch {{ }}
  }}
}}
"""
    run_ps(script, logger=logger, dry_run=dry_run)

def disk_zero_fill_diskpart(disk_number: int, logger: logging.Logger, dry_run: bool = False) -> None:
    # Warning: very slow (writes zeros across the whole device)
    script = f"""
$script = @"
select disk {disk_number}
clean all
"@
$path = [System.IO.Path]::GetTempFileName()
Set-Content -Path $path -Value $script -Encoding ASCII
try {{
  diskpart /s $path
}} finally {{
  Remove-Item $path -ErrorAction SilentlyContinue
}}
"""
    run_ps(script, logger=logger, dry_run=dry_run, timeout=None)

def clear_and_init(disk_number: int, style: str, wipe: str, logger: logging.Logger, dry_run: bool = False) -> None:
    # wipe: "metadata" (Clear-Disk) | "zero-all" (diskpart clean all) | "none"
    if wipe == "zero-all":
        logger.info("Wipe: full zero-fill (diskpart clean all). This can take a long time...")
        disk_zero_fill_diskpart(disk_number, logger=logger, dry_run=dry_run)
    logger.info("Clearing metadata and initializing disk...")
    script = f"""
$disk = Get-Disk -Number {disk_number}
if ($null -eq $disk) {{ throw "Disk not found" }}
if ($disk.IsReadOnly) {{ Set-Disk -Number {disk_number} -IsReadOnly $false }}
if ($disk.IsOffline)  {{ Set-Disk -Number {disk_number} -IsOffline  $false }}
Clear-Disk -Number {disk_number} -RemoveData -Confirm:$false
Initialize-Disk -Number {disk_number} -PartitionStyle {style}
"""
    run_ps(script, logger=logger, dry_run=dry_run)

def _ps_allocate_letter_block() -> str:
    # PowerShell block to ensure a drive letter is assigned (prefers D:..Z:)
    return r"""
function Get-FreeLetter {
  $used = (Get-Volume -ErrorAction SilentlyContinue | Where-Object {$_.DriveLetter} | ForEach-Object {$_.DriveLetter})
  foreach ($c in [char[]]([int][char]'D'..[int][char]'Z')) {
    if ($used -notcontains $c) { return $c }
  }
  return $null
}
"""

def create_partition_and_format(
    disk_number: int, fs: str, label: str, cluster: Optional[int], quick: bool,
    logger: logging.Logger, dry_run: bool = False
) -> str:
    # Return assigned drive letter
    au = f"-AllocationUnitSize {cluster}" if cluster else ""
    full = "" if quick else "-Full"
    # Use single quotes around label; sanitize_label already removed quotes.
    logger.info("Creating primary partition and formatting...")
    script = f"""
{_ps_allocate_letter_block()}
$part = New-Partition -DiskNumber {disk_number} -UseMaximumSize -AssignDriveLetter
$dl = ($part | Get-Partition | Get-Volume -ErrorAction SilentlyContinue).DriveLetter
if (-not $dl) {{
  $free = Get-FreeLetter
  if ($free) {{
    Set-Partition -DiskNumber {disk_number} -PartitionNumber $($part.PartitionNumber) -NewDriveLetter $free | Out-Null
    $dl = $free
  }} else {{
    throw "No free drive letters available"
  }}
}}
Format-Volume -DriveLetter $dl -FileSystem {fs} -NewFileSystemLabel '{label}' {au} -Force -Confirm:$false {full} | Out-Null
$dl
"""
    p = run_ps(script, logger=logger, dry_run=dry_run)
    drive_letter = (p.stdout or "").strip().splitlines()[-1].strip() if p.stdout else ""
    return drive_letter

def verify_volume(drive_letter: str, expected_fs: str, expected_label: str, logger: logging.Logger, dry_run: bool = False) -> bool:
    if not drive_letter:
        return False
    script = f'$v = Get-Volume -DriveLetter {drive_letter}; if ($v) {{ "$($v.FileSystem)|$($v.FileSystemLabel)" }}'
    p = run_ps(script, logger=logger, dry_run=dry_run)
    out = (p.stdout or "").strip()
    if not out or "|" not in out:
        return False
    fs, label = out.split("|", 1)
    return (fs.upper() == expected_fs.upper() and label == expected_label)

def test_io(drive_letter: str, size_mb: int, logger: logging.Logger, dry_run: bool = False) -> Tuple[bool, str]:
    if dry_run:
        return True, "dry-run"
    if not drive_letter:
        return False, "No drive letter available"
    root = f"{drive_letter}:\\"
    name = f"__fmt_test_{int(time.time())}.bin"
    path = os.path.join(root, name)
    try:
        logger.info(f"I/O test: writing {size_mb} MB...")
        chunk = os.urandom(1024 * 1024)
        total = 0
        with open(path, "wb", buffering=1024 * 1024) as f:
            for _ in range(size_mb):
                f.write(chunk)
                total += len(chunk)
        logger.info("I/O test: validating size...")
        sz = os.path.getsize(path)
        if sz != total:
            return False, f"Size mismatch (read {sz} vs written {total})"
        return True, "OK"
    except Exception as e:
        return False, str(e)
    finally:
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

# ---------------------------
# Orchestration
# ---------------------------

def ensure_safe_target(disk: Dict[str, Any]) -> None:
    if disk["is_system"] or disk["is_boot"]:
        raise PermissionError("Safety lock: selected disk appears to be a system/boot disk.")
    if disk["is_readonly"]:
        raise PermissionError("The disk is read-only. Disable write protection and try again.")
    if disk["size"] and disk["size"] < 64 * 1024 * 1024:
        raise ValueError("Disk is too small to format (less than 64 MB).")

def run_format_pipeline(args: argparse.Namespace, logger: logging.Logger) -> Dict[str, Any]:
    disks = list_disks(logger=logger, dry_run=args.dry_run)
    target = next((d for d in disks if d["number"] == args.disk), None)
    if not target:
        raise ValueError(f"Disk #{args.disk} not found.")
    ensure_safe_target(target)

    # Filesystem selection
    fs = args.fs.upper()
    if fs == "AUTO":
        fs = suggest_fs(target["size"])

    # Label sanitization
    label = sanitize_label(args.label, fs=fs, camera_compat=args.camera_compat)

    # Cluster selection
    cluster: Optional[int] = None
    if args.cluster:
        if isinstance(args.cluster, str) and args.cluster.upper() == "AUTO":
            cluster = suggest_cluster(fs=fs, bytes_size=target["size"])
        elif isinstance(args.cluster, int):
            if args.cluster <= 0:
                raise ValueError("Cluster size must be a positive integer.")
            # You could add more validation (power-of-two, upper bounds), but Format-Volume will enforce constraints.
            cluster = args.cluster

    style = "GPT" if args.gpt else "MBR"

    # Confirmation
    if not args.yes:
        print("")
        print("WARNING: ALL DATA ON THE SELECTED DISK WILL BE ERASED.")
        print(f"- Disk: #{target['number']}  {format_size(target['size'])}  {target['bus_type']}  {target['friendly_name']}")
        if target.get("unique_id"):
            print(f"- UniqueId: {target['unique_id']}")
        print(f"- Partition style: {style}")
        print(f"- File system: {fs}")
        if args.cluster == "AUTO":
            print(f"- Cluster: AUTO ({suggest_cluster(fs, target['size'])})")
        else:
            print(f"- Cluster: {cluster or 'DEFAULT'}")
        print(f"- Label: {label}")
        print(f"- Wipe: {args.wipe}")
        print(f"- Mode: {'QUICK' if args.quick else 'FULL'}")
        conf = input(f"Type CONFIRM-{target['number']} to proceed: ").strip()
        if conf != f"CONFIRM-{target['number']}":
            raise RuntimeError("Confirmation mismatch. Operation cancelled.")

    # Execute
    t0 = time.time()
    result: Dict[str, Any] = {
        "app": APP_NAME,
        "version": APP_VERSION,
        "disk": target["number"],
        "size": target["size"],
        "bus_type": target["bus_type"],
        "friendly_name": target["friendly_name"],
        "unique_id": target.get("unique_id", ""),
        "fs": fs,
        "style": style,
        "label": label,
        "cluster": cluster or "DEFAULT",
        "quick": args.quick,
        "wipe": args.wipe,
        "camera_compat": args.camera_compat,
        "dry_run": args.dry_run,
        "steps": [],
    }

    try:
        logger.info("Dismounting any mounted volumes on the target disk...")
        dismount_volumes(target["number"], logger=logger, dry_run=args.dry_run)
        result["steps"].append("dismount_volumes")

        clear_and_init(target["number"], style=style, wipe=args.wipe, logger=logger, dry_run=args.dry_run)
        result["steps"].append("clear_and_init")

        dl = create_partition_and_format(
            disk_number=target["number"], fs=fs, label=label, cluster=cluster, quick=args.quick,
            logger=logger, dry_run=args.dry_run
        )
        result["drive_letter"] = dl
        result["steps"].append("create_partition_and_format")

        if not args.skip_verify:
            logger.info("Verifying filesystem and label...")
            ok = verify_volume(dl, expected_fs=fs, expected_label=label, logger=logger, dry_run=args.dry_run)
            result["verify"] = "OK" if ok else "FAILED"
            if not ok:
                raise RuntimeError("Post-format verification failed.")

        if args.test_io:
            ok, msg = test_io(result.get("drive_letter", ""), size_mb=args.test_io_size, logger=logger, dry_run=args.dry_run)
            result["test_io"] = {"ok": ok, "message": msg, "size_mb": args.test_io_size}
            if not ok:
                raise RuntimeError(f"I/O test failed: {msg}")

        result["status"] = "OK"
        result["duration_sec"] = round(time.time() - t0, 2)
        logger.info("Operation completed successfully.")
        return result

    except Exception as e:
        result["status"] = "ERROR"
        result["error"] = str(e)
        result["duration_sec"] = round(time.time() - t0, 2)
        logger.error(f"Error: {e}")
        return result

# ---------------------------
# CLI
# ---------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="sdFormatter.py",
        description="Professional SD Formatter for Windows. USE WITH CAUTION: this will erase the selected disk."
    )
    ap.add_argument("--version", action="store_true", help="Print version and exit")
    ap.add_argument("--list", action="store_true", help="List available disks")
    ap.add_argument("--info", type=int, help="Show detailed info for the specified disk number")
    ap.add_argument("--disk", type=int, help="Target disk number (from Get-Disk Number)")
    ap.add_argument("--label", type=str, help="Volume label")
    ap.add_argument("--fs", type=str, default="AUTO", choices=["AUTO", "FAT32", "exFAT", "EXFAT", "fat32"], help="Filesystem")
    ap.add_argument("--quick", action="store_true", help="Quick format (default). Use --full for full format")
    ap.add_argument("--full", action="store_true", help="Full format (surface check, slower)")
    ap.add_argument("--cluster", type=str, help='Cluster size in bytes (e.g., 32768) or "AUTO" for suggested')
    ap.add_argument("--gpt", action="store_true", help="Use GPT (default is MBR)")
    ap.add_argument("--camera-compat", action="store_true", help="Camera-friendly label (uppercase, max 11, A-Z 0-9 space underscore)")
    ap.add_argument("--wipe", type=str, default="metadata", choices=["none", "metadata", "zero-all"], help="Wipe type before init")
    ap.add_argument("--test-io", action="store_true", help="Run an I/O test after formatting")
    ap.add_argument("--test-io-size", type=int, default=8, help="I/O test size in MB (default 8)")
    ap.add_argument("--report", type=str, help="Write a JSON report to the given path")
    ap.add_argument("--dry-run", action="store_true", help="Simulation mode: do not modify anything, print planned steps/PS commands")
    ap.add_argument("--yes", action="store_true", help="Skip interactive confirmation")
    ap.add_argument("--no-elevate", action="store_true", help="Do not attempt auto-elevation (UAC)")
    ap.add_argument("-v", "--verbose", action="count", default=0, help="Increase verbosity (can be repeated)")
    args = ap.parse_args()

    if args.version:
        print(f"{APP_NAME} v{APP_VERSION}")
        sys.exit(0)

    # Normalize quick/full (quick is default)
    if args.full:
        args.quick = False
    else:
        args.quick = True if not args.quick else True

    # Normalize FS
    args.fs = args.fs.upper()

    # Normalize cluster
    if args.cluster:
        if args.cluster.upper() == "AUTO":
            args.cluster = "AUTO"
        else:
            try:
                c = int(args.cluster)
                if c <= 0:
                    raise ValueError()
                args.cluster = c
            except Exception:
                raise SystemExit("Invalid --cluster value. Use a positive integer or 'AUTO'.")
    return args

def write_report(report_path: str, data: Dict[str, Any], logger: logging.Logger) -> None:
    try:
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info(f"Report written: {report_path}")
    except Exception as e:
        logger.error(f"Failed to write report: {e}")

def main() -> int:
    if os.name != "nt":
        print("This tool is designed for Windows.")
        return 3
    args = parse_args()
    logger = setup_logger(args.verbose)

    if args.list or args.info is not None:
        # Non-destructive operations: no elevation required
        disks = list_disks(logger=logger, dry_run=args.dry_run)
        print_disk_table(disks)
        if args.info is not None:
            d = next((x for x in disks if x["number"] == args.info), None)
            if d:
                print("\nDisk details:")
                print(json.dumps(d, indent=2))
            else:
                print(f"\nDisk #{args.info} not found.")
        return 0

    # Destructive operations: elevate
    relaunch_as_admin(logger, no_elevate=args.no_elevate)
    if not is_admin():
        logger.error("Insufficient privileges. Please run as Administrator.")
        return 2

    if args.disk is None or not args.label:
        print("Missing required parameters: --disk and --label. Use --list to view disks.")
        return 1

    res = run_format_pipeline(args, logger)
    if args.report:
        write_report(args.report, res, logger)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0 if res.get("status") == "OK" else 1

if __name__ == "__main__":
    sys.exit(main())
