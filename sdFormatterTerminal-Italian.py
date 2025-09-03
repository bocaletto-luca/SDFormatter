# nomeapp.py
# SD Formatter professionale (Windows). Single-file, Python 3.10+
# Funzioni chiave:
# - Elenco e dettagli dischi (con volumi/lettere)
# - Formattazione sicura (MBR di default, 1 partizione, FAT32/exFAT, quick/full)
# - Wipe selettivo (metadati o "clean all" zero-fill)
# - Verifica post-format e test I/O opzionale
# - Log rotante e report JSON
# - Auto-elevazione UAC (disattivabile), modalità simulazione (dry-run)
#
# Uso rapido:
#   python nomeapp.py --list
#   python nomeapp.py --disk 3 --label SD --fs AUTO --full --camera-compat --yes
#   python nomeapp.py --disk 3 --label GOPRO --fs AUTO --cluster AUTO --test-io --report report.json --yes
#
# Nota: operazioni su disco richiedono privilegi amministrativi.

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional, Tuple

APP_NAME = "SDFormatterPro"
POWERSHELL = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"]

# ---------------------------
# Utilità: logging e UAC
# ---------------------------

def get_log_path() -> str:
    base = os.getenv("LOCALAPPDATA") or os.getcwd()
    log_dir = os.path.join(base, APP_NAME)
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, "app.log")

def setup_logger(verbosity: int) -> logging.Logger:
    logger = logging.getLogger(APP_NAME)
    logger.setLevel(logging.DEBUG)
    # Console
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if verbosity > 0 else logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(ch)
    # File
    fh = RotatingFileHandler(get_log_path(), maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)
    return logger

def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False

def relaunch_as_admin(logger: logging.Logger, no_elevate: bool) -> None:
    if is_admin() or no_elevate:
        return
    logger.info("Richiesti privilegi amministrativi: richiesta elevazione UAC...")
    try:
        params = subprocess.list2cmdline(sys.argv[1:])
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, f'"{os.path.abspath(sys.argv[0])}" {params}', None, 1
        )
        # ShellExecuteW ritorna >32 se ok; altrimenti è un errore
        if ret <= 32:
            raise RuntimeError("Elevazione negata o fallita")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Impossibile elevare i privilegi automaticamente: {e}")
        logger.error("Rilancia il comando da un prompt Amministratore, oppure usa --no-elevate per continuare senza.")
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
        # Simulazione: non esegue
        class Dummy:
            returncode = 0
            stdout = ""
            stderr = ""
        return Dummy()  # type: ignore
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if p.returncode != 0:
        logger.debug(f"[PS:stderr] {p.stderr.strip()}")
        raise PSError(p.stderr.strip() or "PowerShell error")
    if p.stdout:
        logger.debug(f"[PS:stdout] {p.stdout.strip()}")
    return p

def ps_json(script: str, logger: logging.Logger, dry_run: bool = False) -> Any:
    # Forza ConvertTo-Json con Depth alto per oggetti annidati
    s = f"({script}) | ConvertTo-Json -Depth 6 -Compress"
    p = run_ps(s, logger=logger, dry_run=dry_run)
    text = (p.stdout or "").strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Ri-prova rimuovendo eventuali BOM/rumore
        cleaned = text.strip("\ufeff \n\r\t")
        return json.loads(cleaned) if cleaned else []

# ---------------------------
# Enumerazione dischi
# ---------------------------

def list_disks(logger: logging.Logger, dry_run: bool = False) -> List[Dict[str, Any]]:
    discs = ps_json(
        "Get-Disk | Select-Object Number,Size,BusType,FriendlyName,IsSystem,IsBoot,IsReadOnly,IsOffline",
        logger, dry_run=dry_run
    )
    if isinstance(discs, dict):
        discs = [discs]
    results: List[Dict[str, Any]] = []
    for d in discs:
        number = int(d["Number"])
        parts = ps_json(
            f"Get-Partition -DiskNumber {number} -ErrorAction SilentlyContinue | "
            "Select-Object DiskNumber,PartitionNumber,DriveLetter,Size,Type",
            logger, dry_run=dry_run
        )
        if isinstance(parts, dict):
            parts = [parts]
        # Lettere volume
        letters: List[str] = []
        if parts:
            # Mappa a lettere disponibili
            vol_letters = ps_json(
                f"(Get-Partition -DiskNumber {number} -ErrorAction SilentlyContinue | Get-Volume -ErrorAction SilentlyContinue | "
                "Select-Object DriveLetter) ", logger, dry_run=dry_run
            )
            if isinstance(vol_letters, dict):
                vol_letters = [vol_letters]
            for v in vol_letters or []:
                dl = str(v.get("DriveLetter") or "").strip()
                if dl:
                    letters.append(dl)
        results.append({
            "number": number,
            "size": int(d["Size"]),
            "bus_type": str(d.get("BusType") or ""),
            "friendly_name": str(d.get("FriendlyName") or ""),
            "is_system": bool(d.get("IsSystem", False)),
            "is_boot": bool(d.get("IsBoot", False)),
            "is_readonly": bool(d.get("IsReadOnly", False)),
            "is_offline": bool(d.get("IsOffline", False)),
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
        print("Nessun disco trovato.")
        return
    headers = ["#", "Size", "Bus", "SYS", "BOOT", "RO", "OFF", "Name", "Letters"]
    widths = [4, 10, 8, 4, 5, 3, 3, 20, 10]
    def fmt(cols: List[str]) -> str:
        return " ".join(str(c).ljust(w) for c, w in zip(cols, widths))
    print(fmt(headers))
    print("-" * sum(widths))
    for d in disks:
        row = [
            str(d["number"]),
            format_size(d["size"]),
            d["bus_type"],
            "Y" if d["is_system"] else "",
            "Y" if d["is_boot"] else "",
            "Y" if d["is_readonly"] else "",
            "Y" if d["is_offline"] else "",
            (d["friendly_name"] or "")[:20],
            ",".join(d["letters"]) if d["letters"] else "-",
        ]
        print(fmt(row))

# ---------------------------
# Politiche FS e cluster
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
    # exFAT
    if gb <= 64:
        return 131072
    elif gb <= 256:
        return 262144
    else:
        return 524288

# ---------------------------
# Label e profili compat
# ---------------------------

INVALID_LABEL_CHARS = r'<>:"/\\|?*'

def sanitize_label(label: str, fs: str, camera_compat: bool) -> str:
    lbl = label.strip()
    if camera_compat:
        base = re.sub(rf"[^{re.escape('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _')}]","_", lbl.upper())
        base = re.sub(r"\s+", " ", base).strip()
        base = base[:11] or "SD_CARD"
        return base
    # Generale (Windows): rimuovi caratteri non permessi, max 32
    base = "".join(c for c in lbl if (32 <= ord(c) < 127) and c not in INVALID_LABEL_CHARS)
    base = base.strip(". ")[:32]
    return base or "SD_CARD"

# ---------------------------
# Pipeline formattazione
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
    # ATTENZIONE: molto lento (scrive zeri su tutta la capacità)
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
        logger.info("Wipe: zero-fill dell'intero disco (clean all). Operazione lunga...")
        disk_zero_fill_diskpart(disk_number, logger=logger, dry_run=dry_run)
    logger.info("Pulizia metadati e inizializzazione...")
    script = f"""
$disk = Get-Disk -Number {disk_number}
if ($disk.IsReadOnly) {{ Set-Disk -Number {disk_number} -IsReadOnly $false }}
if ($disk.IsOffline)  {{ Set-Disk -Number {disk_number} -IsOffline  $false }}
Clear-Disk -Number {disk_number} -RemoveData -Confirm:$false
Initialize-Disk -Number {disk_number} -PartitionStyle {style}
"""
    run_ps(script, logger=logger, dry_run=dry_run)

def create_partition_and_format(
    disk_number: int, fs: str, label: str, cluster: Optional[int], quick: bool,
    logger: logging.Logger, dry_run: bool = False
) -> str:
    # Ritorna la lettera assegnata
    au = f"-AllocationUnitSize {cluster}" if cluster else ""
    full = "" if quick else "-Full"
    logger.info("Creazione partizione primaria e formattazione...")
    script = f"""
$part = New-Partition -DiskNumber {disk_number} -UseMaximumSize -AssignDriveLetter
$dl = ($part | Get-Volume).DriveLetter
Format-Volume -DriveLetter $dl -FileSystem {fs} -NewFileSystemLabel "{label}" {au} -Force -Confirm:$false {full} | Out-Null
$dl
"""
    p = run_ps(script, logger=logger, dry_run=dry_run)
    drive_letter = (p.stdout or "").strip().splitlines()[-1].strip() if p.stdout else ""
    return drive_letter

def verify_volume(drive_letter: str, expected_fs: str, expected_label: str, logger: logging.Logger, dry_run: bool = False) -> bool:
    if not drive_letter:
        return False
    script = f'$v = Get-Volume -DriveLetter {drive_letter}; "$($v.FileSystem)|$($v.FileSystemLabel)"'
    p = run_ps(script, logger=logger, dry_run=dry_run)
    out = (p.stdout or "").strip()
    if not out or "|" not in out:
        return False
    fs, label = out.split("|", 1)
    ok = (fs.upper() == expected_fs.upper() and label == expected_label)
    return ok

def test_io(drive_letter: str, size_mb: int, logger: logging.Logger, dry_run: bool = False) -> Tuple[bool, str]:
    if dry_run:
        return True, "dry-run"
    root = f"{drive_letter}:\\"
    name = f"__fmt_test_{int(time.time())}.bin"
    path = os.path.join(root, name)
    try:
        logger.info(f"Test I/O: scrittura {size_mb} MB ...")
        chunk = os.urandom(1024 * 1024)
        total = 0
        with open(path, "wb", buffering=1024 * 1024) as f:
            for _ in range(size_mb):
                f.write(chunk)
                total += len(chunk)
        logger.info("Test I/O: lettura e verifica dimensione...")
        sz = os.path.getsize(path)
        if sz != total:
            return False, f"Dimensione letta {sz} diversa da scritta {total}"
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
# Orchestrazione
# ---------------------------

def ensure_safe_target(disk: Dict[str, Any]) -> None:
    if disk["is_system"] or disk["is_boot"]:
        raise PermissionError("Protezione attiva: il disco selezionato risulta di sistema/boot.")
    if disk["is_readonly"]:
        raise PermissionError("Il disco è in sola lettura. Rimuovi l'interruttore di protezione scrittura e riprova.")

def run_format_pipeline(
    args: argparse.Namespace, logger: logging.Logger
) -> Dict[str, Any]:
    disks = list_disks(logger=logger, dry_run=args.dry_run)
    target = next((d for d in disks if d["number"] == args.disk), None)
    if not target:
        raise ValueError(f"Disco #{args.disk} non trovato.")
    ensure_safe_target(target)

    # FS e cluster
    fs = args.fs.upper()
    if fs == "AUTO":
        fs = suggest_fs(target["size"])
    label = sanitize_label(args.label, fs=fs, camera_compat=args.camera_compat)
    cluster: Optional[int] = None
    if args.cluster:
        if isinstance(args.cluster, str) and args.cluster.upper() == "AUTO":
            cluster = suggest_cluster(fs=fs, bytes_size=target["size"])
        elif isinstance(args.cluster, int):
            cluster = args.cluster

    style = "GPT" if args.gpt else "MBR"

    # Conferma finale
    if not args.yes:
        print("")
        print("ATTENZIONE: TUTTI I DATI SUL DISCO SELEZIONATO SARANNO CANCELLATI.")
        print(f"- Disco: #{target['number']}  {format_size(target['size'])}  {target['bus_type']}  {target['friendly_name']}")
        print(f"- Stile partizione: {style}")
        print(f"- File system: {fs}")
        print(f"- Cluster: {'AUTO('+str(suggest_cluster(fs, target['size']))+')' if args.cluster == 'AUTO' else (cluster or 'DEFAULT')}")
        print(f"- Etichetta: {label}")
        print(f"- Wipe: {args.wipe}")
        print(f"- Modalità: {'QUICK' if args.quick else 'FULL'}")
        conf = input(f"Digita CONFIRM-{target['number']} per procedere: ").strip()
        if conf != f"CONFIRM-{target['number']}":
            raise RuntimeError("Conferma non valida. Operazione annullata.")

    # Operazioni
    t0 = time.time()
    result: Dict[str, Any] = {
        "disk": target["number"],
        "size": target["size"],
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
        logger.info("Smontaggio volumi...")
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
            logger.info("Verifica volume...")
            ok = verify_volume(dl, expected_fs=fs, expected_label=label, logger=logger, dry_run=args.dry_run)
            result["verify"] = "OK" if ok else "FAILED"
            if not ok:
                raise RuntimeError("Verifica post-format fallita.")

        if args.test_io:
            ok, msg = test_io(result.get("drive_letter", ""), size_mb=args.test_io_size, logger=logger, dry_run=args.dry_run)
            result["test_io"] = {"ok": ok, "message": msg, "size_mb": args.test_io_size}
            if not ok:
                raise RuntimeError(f"Test I/O fallito: {msg}")

        result["status"] = "OK"
        result["duration_sec"] = round(time.time() - t0, 2)
        logger.info("Operazione completata con successo.")
        return result

    except Exception as e:
        result["status"] = "ERROR"
        result["error"] = str(e)
        result["duration_sec"] = round(time.time() - t0, 2)
        logger.error(f"Errore: {e}")
        return result

# ---------------------------
# CLI
# ---------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="nomeapp.py",
        description="SD Formatter professionale (Windows). Usa con cautela: cancella i dati sul disco selezionato."
    )
    ap.add_argument("--list", action="store_true", help="Elenca i dischi disponibili")
    ap.add_argument("--info", type=int, help="Mostra dettagli del disco specificato (numero)")
    ap.add_argument("--disk", type=int, help="Numero del disco target (da Get-Disk Number)")
    ap.add_argument("--label", type=str, help="Etichetta volume")
    ap.add_argument("--fs", type=str, default="AUTO", choices=["AUTO", "FAT32", "exFAT", "EXFAT", "fat32"], help="File system")
    ap.add_argument("--quick", action="store_true", help="Formattazione rapida (default). Se omesso e si usa --full, farà full.")
    ap.add_argument("--full", action="store_true", help="Formattazione completa (non quick)")
    ap.add_argument("--cluster", type=str, help='Dimensione cluster in byte (es. 32768) oppure "AUTO" per suggerito')
    ap.add_argument("--gpt", action="store_true", help="Usa GPT (default MBR)")
    ap.add_argument("--camera-compat", action="store_true", help="Applica label compatibile con fotocamere (FAT-style, max 11 char, uppercase)")
    ap.add_argument("--wipe", type=str, default="metadata", choices=["none", "metadata", "zero-all"], help="Tipo di wipe prima dell'inizializzazione")
    ap.add_argument("--test-io", action="store_true", help="Esegue test I/O dopo la formattazione")
    ap.add_argument("--test-io-size", type=int, default=8, help="Dimensione del test I/O in MB (default 8)")
    ap.add_argument("--report", type=str, help="Scrive un report JSON sul percorso indicato")
    ap.add_argument("--dry-run", action="store_true", help="Modalità simulazione: non esegue modifiche, stampa i passi/PS")
    ap.add_argument("--yes", action="store_true", help="Salta la conferma interattiva")
    ap.add_argument("--no-elevate", action="store_true", help="Non tentare l'auto-elevazione UAC")
    ap.add_argument("-v", "--verbose", action="count", default=0, help="Aumenta verbosità (ripetibile)")
    args = ap.parse_args()

    # Normalizza flags quick/full
    if args.full:
        args.quick = False
    else:
        # default quick se non specificato
        args.quick = True if not args.quick else True

    # Normalizza FS
    args.fs = args.fs.upper()

    # Normalizza cluster
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
                raise SystemExit("Valore --cluster non valido. Usa intero positivo o 'AUTO'.")
    return args

def write_report(report_path: str, data: Dict[str, Any], logger: logging.Logger) -> None:
    try:
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info(f"Report scritto: {report_path}")
    except Exception as e:
        logger.error(f"Impossibile scrivere il report: {e}")

def main() -> int:
    if os.name != "nt":
        print("Questo tool è progettato per Windows.")
        return 3
    args = parse_args()
    logger = setup_logger(args.verbose)

    if args.list or args.info is not None:
        # Operazioni non distruttive: nessuna elevazione obbligatoria
        disks = list_disks(logger=logger, dry_run=args.dry_run)
        print_disk_table(disks)
        if args.info is not None:
            d = next((x for x in disks if x["number"] == args.info), None)
            if d:
                print("\nDettagli disco:")
                print(json.dumps(d, indent=2))
            else:
                print(f"\nDisco #{args.info} non trovato.")
        return 0

    # Operazioni distruttive: eleva
    relaunch_as_admin(logger, no_elevate=args.no_elevate)
    if not is_admin():
        logger.error("Permessi insufficienti. Esegui come Amministratore.")
        return 2

    if args.disk is None or not args.label:
        print("Parametri obbligatori mancanti: --disk e --label. Usa --list per vedere i dischi.")
        return 1

    res = run_format_pipeline(args, logger)
    if args.report:
        write_report(args.report, res, logger)
    # Stampa risultato JSON su stdout (utile per automazione)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0 if res.get("status") == "OK" else 1

if __name__ == "__main__":
    sys.exit(main())
