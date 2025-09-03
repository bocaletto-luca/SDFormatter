import sys
import os
import json
import ctypes
import subprocess
import shutil
from dataclasses import dataclass
from typing import List, Dict, Any, Optional

from PySide6.QtCore import Qt, QThread, Signal, QSize
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QTableWidget, QTableWidgetItem, QLabel, QLineEdit, QComboBox, QCheckBox,
    QTextEdit, QMessageBox, QProgressBar, QDialog, QDialogButtonBox, QFormLayout,
    QFrame
)


# =========================
# Helpers di sistema
# =========================

def is_windows() -> bool:
    return os.name == "nt"

def is_admin() -> bool:
    if not is_windows():
        return False
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False

def relaunch_as_admin():
    if not is_windows():
        return
    params = " ".join([f'"{arg}"' for arg in sys.argv])
    try:
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, None, 1
        )
        sys.exit(0)
    except Exception as e:
        QMessageBox.critical(None, "Elevazione fallita", f"Non riesco a elevare i privilegi: {e}")

def run_powershell(ps_command: str) -> subprocess.CompletedProcess:
    # Usa powershell.exe in modalità silenziosa
    exe = shutil.which("powershell") or shutil.which("powershell.exe")
    if not exe:
        raise RuntimeError("PowerShell non trovato nel PATH.")
    return subprocess.run(
        [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_command],
        capture_output=True, text=True, encoding="utf-8", errors="replace"
    )

def run_powershell_json(ps_command: str) -> Any:
    cp = run_powershell(ps_command)
    if cp.returncode != 0:
        raise RuntimeError(cp.stderr.strip() or "Errore PowerShell sconosciuto.")
    text = cp.stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"JSON non valido da PowerShell: {e}\nOutput: {text[:1000]}")


def bytes_human(n: int) -> str:
    step = 1024.0
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(n)
    for u in units:
        if size < step:
            return f"{size:.1f} {u}"
        size /= step
    return f"{size:.1f} PB"

def cluster_bytes_from_label(label: str) -> Optional[int]:
    # label es: "Auto", "4 KB", "16 KB", ... "1024 KB"
    if label.lower().startswith("auto"):
        return None
    num = label.split()[0]
    try:
        return int(num) * 1024
    except Exception:
        return None


# =========================
# Backend dischi
# =========================

@dataclass
class DiskInfo:
    number: int
    size: int
    bus_type: str
    friendly_name: str
    is_system: bool
    is_boot: bool
    is_readonly: bool
    partition_style: str
    letters: List[str]

def list_disks() -> List[DiskInfo]:
    if not is_windows():
        raise RuntimeError("Questa app supporta solo Windows.")
    # PowerShell: raccoglie dischi e lettere montate
    ps = r"""
$disks = Get-Disk | Select-Object Number, Size, BusType, FriendlyName, IsSystem, IsBoot, IsReadOnly, PartitionStyle
$result = @()
foreach ($d in $disks) {
    $letters = @()
    try {
        $letters = (Get-Partition -DiskNumber $d.Number | Where-Object { $_.DriveLetter } | Select-Object -ExpandProperty DriveLetter)
    } catch {}
    if ($letters -eq $null) { $letters = @() }
    $obj = [PSCustomObject]@{
        Number = $d.Number
        Size = $d.Size
        BusType = [string]$d.BusType
        FriendlyName = [string]$d.FriendlyName
        IsSystem = [bool]$d.IsSystem
        IsBoot = [bool]$d.IsBoot
        IsReadOnly = [bool]$d.IsReadOnly
        PartitionStyle = [string]$d.PartitionStyle
        Letters = $letters
    }
    $result += $obj
}
$result | ConvertTo-Json -Depth 4
"""
    data = run_powershell_json(ps)
    if data is None:
        return []
    # Normalizza array vs oggetto singolo
    if isinstance(data, dict):
        data = [data]
    disks: List[DiskInfo] = []
    for d in data:
        disks.append(DiskInfo(
            number=int(d.get("Number")),
            size=int(d.get("Size") or 0),
            bus_type=str(d.get("BusType") or ""),
            friendly_name=str(d.get("FriendlyName") or ""),
            is_system=bool(d.get("IsSystem")),
            is_boot=bool(d.get("IsBoot")),
            is_readonly=bool(d.get("IsReadOnly")),
            partition_style=str(d.get("PartitionStyle") or ""),
            letters=list(d.get("Letters") or [])
        ))
    return disks


# =========================
# Worker di formattazione
# =========================

class FormatWorker(QThread):
    progress = Signal(str)        # messaggi
    step = Signal(int)            # 0..100
    finished = Signal(dict)       # risultato finale
    failed = Signal(str)          # errore
    confirm_needed = Signal(str)  # eventuali prompt

    def __init__(self, args: Dict[str, Any]):
        super().__init__()
        self.args = args
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def _emit(self, msg: str, pct: Optional[int] = None):
        self.progress.emit(msg)
        if pct is not None:
            self.step.emit(pct)

    def _check_cancel(self):
        if self._cancelled:
            raise RuntimeError("Operazione annullata dall'utente.")

    def run(self):
        try:
            self._run_pipeline()
        except Exception as e:
            self.failed.emit(str(e))

    def _run_pipeline(self):
        dry_run: bool = self.args.get("dry_run", True)
        disk: int = int(self.args["disk"])
        label: str = self.args.get("label", "").strip()
        fs: str = self.args.get("fs", "AUTO").upper()
        quick: bool = bool(self.args.get("quick", True))
        deep_clean: bool = bool(self.args.get("deep_clean", False))
        cam_compat: bool = bool(self.args.get("cam_compat", False))
        cluster_label: str = self.args.get("cluster_label", "Auto")

        # Determina FS se AUTO e cluster
        # Ottiene dimensione disco per dedurre AUTO
        size_bytes = None
        try:
            size_bytes = next((d.size for d in list_disks() if d.number == disk), None)
        except Exception:
            size_bytes = None

        if fs == "AUTO":
            if size_bytes is not None and size_bytes <= 32 * 1024**3:
                fs = "FAT32"
            else:
                fs = "exFAT"

        # Compat fotocamere forza FAT32 + 32KB se possibile
        if cam_compat:
            if size_bytes is not None and size_bytes <= 32 * 1024**3:
                fs = "FAT32"
            # cluster 32KB preferito per molte fotocamere
            cluster_bytes = 32 * 1024
        else:
            cluster_bytes = cluster_bytes_from_label(cluster_label)

        # Vincoli di sicurezza: vieta FAT32 > 32GB
        if fs == "FAT32" and (size_bytes is None or size_bytes > 32 * 1024**3):
            raise RuntimeError("FAT32 selezionato ma la capacità supera 32 GB. Usa exFAT o AUTO.")

        # Step 0: riepilogo
        self._emit(f"Disco #{disk} — FS: {fs}, Etichetta: '{label}', Quick: {quick}, Pulizia profonda: {deep_clean}, Dry-run: {dry_run}", 0)
        self._check_cancel()

        # Step 1: sblocca disco (online/lettura-scrittura)
        self._emit("Verifica e sblocco disco...", 10)
        if not dry_run:
            ps1 = f"Set-Disk -Number {disk} -IsOffline $false -IsReadOnly $false -ErrorAction Stop"
            cp = run_powershell(ps1)
            if cp.returncode != 0:
                raise RuntimeError(cp.stderr.strip() or "Impossibile sbloccare il disco.")
        self._check_cancel()

        # Step 2: pulizia
        if deep_clean:
            self._emit("Pulizia profonda del disco (può richiedere molto tempo)...", 25)
            if not dry_run:
                ps2 = f"Clear-Disk -Number {disk} -RemoveData -Confirm:$false -ErrorAction Stop"
                cp = run_powershell(ps2)
                if cp.returncode != 0:
                    raise RuntimeError(cp.stderr.strip() or "Errore in Clear-Disk con rimozione dati.")
        else:
            self._emit("Pulizia tabella partizioni...", 25)
            if not dry_run:
                ps2 = f"Clear-Disk -Number {disk} -RemoveData:$false -Confirm:$false -ErrorAction Stop"
                cp = run_powershell(ps2)
                if cp.returncode != 0:
                    raise RuntimeError(cp.stderr.strip() or "Errore in Clear-Disk.")
        self._check_cancel()

        # Step 3: inizializzazione MBR (compatibilità massima per SD)
        self._emit("Inizializzazione MBR...", 40)
        if not dry_run:
            ps3 = f"Initialize-Disk -Number {disk} -PartitionStyle MBR -ErrorAction Stop"
            cp = run_powershell(ps3)
            if cp.returncode != 0:
                raise RuntimeError(cp.stderr.strip() or "Errore in Initialize-Disk.")
        self._check_cancel()

        # Step 4: nuova partizione e lettera
        self._emit("Creazione partizione primaria e assegnazione lettera...", 55)
        drive_letter = "X"
        if not dry_run:
            ps4 = f"(New-Partition -DiskNumber {disk} -UseMaximumSize -AssignDriveLetter -ErrorAction Stop).DriveLetter"
            cp = run_powershell(ps4)
            if cp.returncode != 0:
                raise RuntimeError(cp.stderr.strip() or "Errore in New-Partition.")
            drive_letter = (cp.stdout.strip() or "X").replace(":", "")
            if len(drive_letter) != 1:
                raise RuntimeError(f"Lettera unità non valida: '{drive_letter}'")
        else:
            drive_letter = "Z"
        self._check_cancel()

        # Step 5: format
        self._emit(f"Formattazione {fs} sulla lettera {drive_letter}: ...", 80)
        if not dry_run:
            # Parametri Format-Volume
            full_flag = "$true" if not quick else "$false"
            label_param = f'-NewFileSystemLabel "{label}"' if label else ""
            aus_param = f"-AllocationUnitSize {cluster_bytes}" if cluster_bytes else ""
            ps5 = f'Format-Volume -DriveLetter {drive_letter} -FileSystem {fs} {label_param} {aus_param} -Full:{full_flag} -Force -Confirm:$false -ErrorAction Stop'
            cp = run_powershell(ps5)
            if cp.returncode != 0:
                raise RuntimeError(cp.stderr.strip() or "Errore in Format-Volume.")
        self._check_cancel()

        # Step 6: completato
        self._emit("Operazione completata.", 100)
        self.finished.emit({
            "status": "OK",
            "disk": disk,
            "fs": fs,
            "label": label,
            "drive_letter": drive_letter,
            "dry_run": dry_run
        })


# =========================
# Dialog di conferma
# =========================

class ConfirmDialog(QDialog):
    def __init__(self, parent, disk_number: int, summary_text: str):
        super().__init__(parent)
        self.setWindowTitle("Conferma formattazione")
        self.setModal(True)
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)

        info = QLabel(summary_text)
        info.setWordWrap(True)
        info.setStyleSheet("QLabel { color: #444; }")
        layout.addWidget(info)

        layout.addWidget(self._hline())

        form = QFormLayout()
        self.code_label = QLabel(f"Scrivi esattamente: CONFIRM-{disk_number}")
        self.code_edit = QLineEdit()
        self.code_edit.setPlaceholderText(f"CONFIRM-{disk_number}")
        form.addRow(self.code_label, self.code_edit)
        layout.addLayout(form)

        layout.addWidget(self._hline())

        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        self.buttons.button(QDialogButtonBox.Ok).setText("Conferma")
        self.buttons.button(QDialogButtonBox.Cancel).setText("Annulla")
        self.buttons.accepted.connect(self._on_ok)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def _hline(self):
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        return line

    def _on_ok(self):
        if self.code_edit.text().strip() == self.code_label.text().split(":")[-1].strip():
            self.accept()
        else:
            QMessageBox.warning(self, "Conferma errata", "Il testo inserito non corrisponde. Ricontrolla e riprova.")


# =========================
# Interfaccia principale
# =========================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SD Formatter Pro — GUI")
        self.resize(980, 680)

        self.worker: Optional[FormatWorker] = None

        root = QWidget()
        self.setCentralWidget(root)
        main = QVBoxLayout(root)

        # Banner admin
        self.admin_banner = QLabel("")
        self.admin_banner.setWordWrap(True)
        self.admin_banner.setStyleSheet("QLabel { background:#FFF3CD; color:#7A5E00; border:1px solid #FFECB5; padding:8px; }")
        main.addWidget(self.admin_banner)

        # Tabella dischi
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels([
            "#", "Capacità", "Bus", "Nome", "Lettere", "Sistema", "Boot", "Sola lettura"
        ])
        self.table.setSelectionBehavior(self.table.SelectRows)
        self.table.setEditTriggers(self.table.NoEditTriggers)
        main.addWidget(self.table)

        # Opzioni
        opts = QHBoxLayout()

        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText("Etichetta volume")

        self.fs_combo = QComboBox()
        self.fs_combo.addItems(["AUTO", "FAT32", "exFAT", "NTFS"])

        self.quick_check = QCheckBox("Quick format")
        self.quick_check.setChecked(True)

        self.deep_clean_check = QCheckBox("Pulizia profonda")
        self.cam_check = QCheckBox("Compatibilità fotocamere")

        self.cluster_combo = QComboBox()
        self.cluster_combo.addItems(["Auto", "4 KB", "8 KB", "16 KB", "32 KB", "64 KB", "128 KB", "256 KB", "512 KB", "1024 KB"])

        self.dry_run_check = QCheckBox("Modalità prova (dry-run)")
        self.dry_run_check.setChecked(True)

        opts.addWidget(QLabel("Etichetta:"))
        opts.addWidget(self.label_edit, 2)
        opts.addWidget(QLabel("File system:"))
        opts.addWidget(self.fs_combo)
        opts.addWidget(QLabel("Cluster:"))
        opts.addWidget(self.cluster_combo)
        opts.addWidget(self.quick_check)
        opts.addWidget(self.deep_clean_check)
        opts.addWidget(self.cam_check)
        opts.addWidget(self.dry_run_check)
        main.addLayout(opts)

        # Bottoni
        btns = QHBoxLayout()
        self.refresh_btn = QPushButton("Aggiorna")
        self.format_btn = QPushButton("Formatta")
        self.cancel_btn = QPushButton("Annulla")
        self.cancel_btn.setEnabled(False)
        self.elevate_btn = QPushButton("Riavvia come amministratore")
        btns.addWidget(self.refresh_btn)
        btns.addWidget(self.format_btn)
        btns.addWidget(self.cancel_btn)
        btns.addStretch(1)
        btns.addWidget(self.elevate_btn)
        main.addLayout(btns)

        # Log + progresso
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        main.addWidget(self.log)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        main.addWidget(self.progress)

        # Segnali
        self.refresh_btn.clicked.connect(self.load_disks)
        self.format_btn.clicked.connect(self.on_format)
        self.cancel_btn.clicked.connect(self.on_cancel)
        self.elevate_btn.clicked.connect(relaunch_as_admin)
        self.fs_combo.currentTextChanged.connect(self.on_fs_changed)
        self.cam_check.stateChanged.connect(self.on_cam_changed)

        self.update_admin_banner()
        self.load_disks()

    def update_admin_banner(self):
        if not is_windows():
            self.admin_banner.setText("Questa applicazione funziona solo su Windows.")
            self.elevate_btn.setEnabled(False)
            self.format_btn.setEnabled(False)
            return
        if is_admin():
            self.admin_banner.setText("Esecuzione con privilegi amministrativi.")
            self.elevate_btn.setEnabled(False)
        else:
            self.admin_banner.setText("Privilegi amministrativi mancanti. Per formattare è necessario riavviare come amministratore.")
            self.elevate_btn.setEnabled(True)

    def on_fs_changed(self):
        # Se selezioni NTFS e compat fotocamere, disabilita flag
        if self.fs_combo.currentText().upper() == "NTFS":
            self.cam_check.setChecked(False)
            self.cam_check.setEnabled(False)
        else:
            self.cam_check.setEnabled(True)

    def on_cam_changed(self):
        # Se compat fotocamere, forza cluster 32 KB in UI (
