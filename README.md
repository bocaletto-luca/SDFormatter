# SDFormatter

A complete, cross-interface SD card formatting utility for Windows, designed for safety, flexibility, and ease of use.  
This repository contains three versions of the formatter:

- **`sdFormatterGUI.py`** — English version with a full graphical user interface (GUI) built with PySide6/Qt.
- **`sdFormatterTerminal-english.py`** — English terminal/CLI version.
- **`sdFormatterTerminal-italian.py`** — Italian terminal/CLI version.

---

## Features

### Common to all versions
- **Safe disk selection** — System and boot disks are locked to prevent accidental formatting.
- **File system options** — FAT32, exFAT, NTFS, or AUTO (auto-selects based on capacity).
- **Quick or full format** — Choose between fast formatting or a complete overwrite.
- **Deep clean option** — Securely wipes all data (can take significantly longer).
- **Camera compatibility mode** — Optimized FAT32 with 32 KB clusters for SD cards ≤ 32 GB.
- **Cluster size selection** — Manual or automatic allocation unit size.
- **Dry-run mode** — Simulates the process without making changes.
- **PowerShell backend** — Uses native Windows PowerShell commands for reliability.

### GUI version (`sdFormatterGUI.py`)
- Modern, responsive interface with:
  - Disk table with capacity, bus type, name, letters, and status.
  - Option panels for label, file system, cluster size, and format type.
  - Real-time log output and progress bar.
  - Confirmation dialog requiring typed code before formatting.
- Threaded execution — UI remains responsive during operations.
- Built-in privilege check and one-click restart as administrator.

### Terminal versions
- **English CLI** (`sdFormatterTerminal-english.py`) — Interactive prompts and clear status messages in English.
- **Italian CLI** (`sdFormatterTerminal-italian.py`) — Interactive prompts and messages in Italian.
- Lightweight, no GUI dependencies.

---

## Requirements

- **OS:** Windows 10 or 11
- **Python:** 3.10 or newer
- **PowerShell:** Installed and available in PATH
- **Privileges:** Administrator rights required for actual formatting

### Additional for GUI version
- **PySide6** — Install via:
  ```bash
  pip install PySide6
  ```

---

## Installation

Clone the repository:
```bash
git clone https://github.com/bocaletto-luca/SDFormatter.git
cd SDFormatter
```

Install dependencies:
```bash
pip install -r requirements.txt
```
*(For GUI: ensure `PySide6` is included in `requirements.txt` or install manually.)*

---

## Usage

### GUI version
```bash
python sdFormatterGUI.py
```
- Select a disk from the table.
- Configure options (label, file system, cluster size, quick/full, etc.).
- Confirm by typing the required code.
- Monitor progress and logs in real time.

### Terminal (English)
```bash
python sdFormatterTerminal-english.py
```

### Terminal (Italian)
```bash
python sdFormatterTerminal-italian.py
```

---

## Safety Notes

- **Dry-run mode** is enabled by default in the GUI. Disable it only when ready to perform the actual format.
- Always double-check the selected disk number before confirming.
- Deep clean mode will securely erase all data and may take hours on large drives.

---

## License

This project is released under the MIT License. See [LICENSE](LICENSE) for details.

---

## Author

**Luca Bocaletto**  
[GitHub: bocaletto-luca](https://github.com/bocaletto-luca)
