# TPV / TPC Measurement Software — Teledyne LeCroy WaveJet 334

Desktop app for automated **Transient Photovoltage (TPV)** and **Transient
Photocurrent (TPC)** measurements on solar cells (perovskite, OPV, …). It drives
a WaveJet 334 oscilloscope and an Arduino-controlled beam dumper, then saves
processed waveforms as CSV + XLSX.

Full technical documentation: [DOCUMENTACAO.md](DOCUMENTACAO.md) (also under
**Help → Documentation**, F1, in the app).

## Installation

### Option A — Windows executable (recommended for lab PCs)

1. Download **`TPV-TPC.exe`** from the
   [latest release](https://github.com/Polimante/TPV-TPC-analysis/releases/latest).
2. Put it in a folder you can write to (e.g. `Documents\TPV-TPC`), **not** in
   `Program Files`, so it can update itself.
3. Double-click it. Python is not required.

Windows SmartScreen may warn that the file is from an unknown publisher the
first time. Click **More info → Run anyway**.

The PC still needs the **oscilloscope USB/VISA driver** (NI-VISA or the
LeCroy driver) and, for some Arduino clones, the CH340 serial driver.

### Option B — From source

```bash
pip install -r requirements.txt
cd tpv_app
python main.py
```

## Updates

The app checks GitHub for a newer release every time it starts, and on demand
under **Help → Check for updates…**. When an update is available it shows the
release notes and offers:

- **Update now** (exe only): downloads the new version, replaces the exe and
  restarts the program.
- **Skip this version**: stops the automatic check from asking about this version again.
- **Later**: ask again next start.

A copy running from source only reports new versions; update it with `git pull`.
The check never runs a download on its own, and an update is refused while a
measurement is running.

## Hardware setup

Configure timebase, V/div and trigger on the oscilloscope's front panel before
collecting data.

| Channel | Signal | Termination |
|---|---|---|
| **CH1** | Laser Q-switch trigger (t = 0 reference) | 1 MΩ |
| **CH2** | Cell current — TPC | 50 Ω |
| **CH3** | Cell voltage — TPV | 1 MΩ |

**CH1, CH2 and CH3 must all be turned on** on the scope, even if the cell is
only connected to one of them. The software always downloads all three and
stops with *"No data received from oscilloscope channel(s): …"* if one is off.

Instruments are found automatically:

- **Oscilloscope**: every VISA resource is queried with `*IDN?` and the one
  answering "LECROY" is used.
- **Arduino**: matched by USB VID `0x2341` (official) or `0x1A86` (CH340), or a
  port description containing "Arduino", "CH340" or "ATMEGA".

## Measurement sequence

1. Configure the scope (memory depth, data format, averaging).
2. Close the beam dumper and wait the settling time (V_oc stabilisation).
3. Open the beam dumper and acquire (single or N averages).
4. Close the beam dumper immediately.
5. Download CH1–CH3, process, save and plot.

## Output files

Each measurement writes `<sample>_<TPV|TPC>_<timestamp>.csv` and `.xlsx`.

| Column | Description |
|---|---|
| `Time (s)` | Raw time axis |
| `t aligned (s)` | Time with t = 0 at the laser pulse (detected on CH1) |
| `CH1 (V)`, `CH2 (A)`, `CH3 (V)` | Raw channels |
| `ΔCH3 (V)` / `ΔCH2 (A)` | Active channel minus baseline (mean of first 5 %) |
| `Normalized` | Δ signal divided by its peak |

The XLSX adds a **Parameters** sheet with the measurement metadata.

## Project structure

```
tpv_app/
├── main.py           # entry point
├── gui.py            # main window (PyQt6 + pyqtgraph)
├── hardware.py       # WaveJet (PyVISA) and Arduino (pyserial) control
├── core.py           # processing and CSV/XLSX export
├── updater.py        # update check and self-update via GitHub Releases
├── version.py        # current version number
├── diag_osc.py       # standalone scope diagnostics (no GUI)
├── styles.qss        # stylesheet
├── build.ps1         # builds dist\TPV-TPC.exe
├── release.ps1       # publishes a new release on GitHub
├── DOCUMENTACAO.md   # full documentation
└── requirements.txt
```

## Publishing a new version (maintainers)

Requires Python with the requirements installed and the
[GitHub CLI](https://cli.github.com/) logged in (`gh auth login`).

1. Make and commit your changes.
2. Increase `__version__` in `version.py` (e.g. `1.0.0` → `1.1.0`) and commit.
3. Run:

   ```powershell
   powershell -ExecutionPolicy Bypass -File release.ps1 -Notes "What changed in this version"
   ```

   This builds the exe, pushes the code, and creates release `v1.1.0` on GitHub
   with the exe attached. Every installed copy will offer the update the next
   time it starts.

To only build the exe without publishing: `powershell -ExecutionPolicy Bypass -File build.ps1`.
