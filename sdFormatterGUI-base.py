import sys
import json
import threading
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QTableWidget, QTableWidgetItem, QLabel, QLineEdit,
    QComboBox, QCheckBox, QSpinBox, QTextEdit, QMessageBox, QProgressBar
)
from PySide6.QtCore import Qt, Signal, QObject

# Importa qui le funzioni dal tuo sdFormatter.py
# Per esempio:
# from sdFormatter import list_disks, run_format_pipeline, setup_logger, parse_args

# Per demo, creo mock
def list_disks(logger=None, dry_run=False):
    return [
        {"number": 3, "size": 32000000000, "bus_type": "USB", "friendly_name": "SD Card", "letters": ["E"], "is_system": False, "is_boot": False, "is_readonly": False},
        {"number": 4, "size": 64000000000, "bus_type": "USB", "friendly_name": "SDXC", "letters": ["F"], "is_system": False, "is_boot": False, "is_readonly": False}
    ]

def run_format_pipeline(args, logger):
    import time
    time.sleep(2)
    return {"status": "OK", "disk": args["disk"], "fs": args["fs"], "label": args["label"]}

class WorkerSignals(QObject):
    finished = Signal(dict)
    error = Signal(str)
    progress = Signal(str)

class FormatWorker(threading.Thread):
    def __init__(self, args, signals):
        super().__init__()
        self.args = args
        self.signals = signals

    def run(self):
        try:
            self.signals.progress.emit("Starting format...")
            result = run_format_pipeline(self.args, None)
            self.signals.finished.emit(result)
        except Exception as e:
            self.signals.error.emit(str(e))

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SD Formatter Pro - GUI")
        self.resize(800, 600)

        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget)

        # Disk table
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["#", "Size", "Bus", "Name", "Letters"])
        layout.addWidget(self.table)

        # Options
        opt_layout = QHBoxLayout()
        self.label_edit = QLineEdit()
        self.fs_combo = QComboBox()
        self.fs_combo.addItems(["AUTO", "FAT32", "exFAT"])
        self.quick_check = QCheckBox("Quick Format")
        self.quick_check.setChecked(True)
        self.cluster_spin = QSpinBox()
        self.cluster_spin.setMaximum(1048576)
        self.cluster_spin.setValue(0)
        opt_layout.addWidget(QLabel("Label:"))
        opt_layout.addWidget(self.label_edit)
        opt_layout.addWidget(QLabel("FS:"))
        opt_layout.addWidget(self.fs_combo)
        opt_layout.addWidget(self.quick_check)
        opt_layout.addWidget(QLabel("Cluster:"))
        opt_layout.addWidget(self.cluster_spin)
        layout.addLayout(opt_layout)

        # Buttons
        btn_layout = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh")
        self.format_btn = QPushButton("Format")
        btn_layout.addWidget(self.refresh_btn)
        btn_layout.addWidget(self.format_btn)
        layout.addLayout(btn_layout)

        # Log
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        layout.addWidget(self.log_area)

        # Progress
        self.progress = QProgressBar()
        layout.addWidget(self.progress)

        # Signals
        self.refresh_btn.clicked.connect(self.load_disks)
        self.format_btn.clicked.connect(self.start_format)

        self.load_disks()

    def load_disks(self):
        self.table.setRowCount(0)
        disks = list_disks()
        for d in disks:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(str(d["number"])))
            self.table.setItem(row, 1, QTableWidgetItem(f"{d['size']/1e9:.1f} GB"))
            self.table.setItem(row, 2, QTableWidgetItem(d["bus_type"]))
            self.table.setItem(row, 3, QTableWidgetItem(d["friendly_name"]))
            self.table.setItem(row, 4, QTableWidgetItem(",".join(d["letters"])))

    def start_format(self):
        selected = self.table.currentRow()
        if selected < 0:
            QMessageBox.warning(self, "No disk", "Please select a disk to format.")
            return
        disk_number = int(self.table.item(selected, 0).text())
        label = self.label_edit.text().strip()
        if not label:
            QMessageBox.warning(self, "No label", "Please enter a volume label.")
            return
        args = {
            "disk": disk_number,
            "label": label,
            "fs": self.fs_combo.currentText(),
            "quick": self.quick_check.isChecked(),
            "cluster": self.cluster_spin.value() or None
        }
        self.log_area.append(f"Formatting disk #{disk_number}...")
        self.progress.setValue(0)
        self.worker_signals = WorkerSignals()
        self.worker_signals.progress.connect(self.on_progress)
        self.worker_signals.finished.connect(self.on_finished)
        self.worker_signals.error.connect(self.on_error)
        self.worker = FormatWorker(args, self.worker_signals)
        self.worker.start()

    def on_progress(self, msg):
        self.log_area.append(msg)
        self.progress.setValue(self.progress.value() + 20)

    def on_finished(self, result):
        self.progress.setValue(100)
        self.log_area.append(f"Done: {json.dumps(result)}")
        if result.get("status") == "OK":
            QMessageBox.information(self, "Success", "Formatting completed successfully.")
        else:
            QMessageBox.critical(self, "Error", f"Formatting failed: {result}")

    def on_error(self, err):
        QMessageBox.critical(self, "Error", err)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
