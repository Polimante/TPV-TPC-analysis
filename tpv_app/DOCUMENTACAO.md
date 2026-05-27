# TPV/TPC Software — Documentation

Software for automated **TPV (Transient Photovoltage)** and
**TPC (Transient Photocurrent)** measurements on solar cells (perovskite or others)
using the **Teledyne LeCroy WaveJet 334** oscilloscope and an Arduino for
*beam dumper* control.

---

## 1. Hardware

| Component | Function |
|---|---|
| WaveJet 334 (4-channel, 350 MHz, 2 GS/s) | Waveform acquisition |
| Arduino Mega 2560 | Beam dumper control (servo) |
| Solar simulator | Continuous bias light |
| Pulsed laser (Nd:YAG or similar with Q-Switch) | Perturbation pulse |
| Solar cell (perovskite, OPV, etc.) | DUT |

**Channel connections:**

| Channel | Signal | Termination | Use |
|---|---|---|---|
| **CH1** | Laser Q-Switch trigger | 1 MΩ | Time reference (t=0) |
| **CH2** | Cell current (TPC) | **50 Ω** | Measures I via V = I × 50 |
| **CH3** | Cell voltage (TPV) | **1 MΩ** | Measures V_oc + ΔV |

For a given measurement, the cell is **physically connected to only ONE channel**:
- **TPV**: cell on CH3, open-circuit mode (1 MΩ)
- **TPC**: cell on CH2, short-circuit via 50 Ω

CH1 remains connected to the laser trigger for ALL measurements.

---

## 2. Automated Measurement Sequence

When the user clicks **"Collect Data"**, the software executes automatically:

```
1.  Configure scope:
    STOP → MLEN <depth> → DTFORM <BYTE|WORD> → [DTBORD L/H if WORD]
         → ACQ NORMAL|AVERAGE → [AVGCNT N if averaging]

2.  Close beam dumper  ← safety: blocks laser while settling

3.  Wait settling_time (default 5 s)
    ← allows V_oc to stabilise under bias light

4.  Open beam dumper  ← laser pulses start hitting the cell

5.  WSGL? (scope arms in SINGLE mode and blocks until N averages done)
    ← for ACQ NORMAL (N=1): waits for one trigger
    ← for ACQ AVERAGE (N>1): waits until all N averages accumulated

6.  Close beam dumper IMMEDIATELY on completion (or on timeout)

7.  Download waveforms CH1, CH2, CH3

8.  Process and save CSV + XLSX
```

> **Degradation warning**: bias light + laser for many seconds can degrade
> perovskite. Recommended: 32–64 averages, total exposure < 20 s.
> Set `Acq. timeout` ≤ 20 s when working with perovskite.

---

## 3. Data Processing

The software applies three automatic processing steps after each measurement:

### 3.1. Baseline subtraction

The active channel signal is shifted to start at zero:

```
ΔV(t) = V(t) − baseline
```

The **baseline** is the mean of the **first 5%** of the trace (pre-pulse region).

- **TPV**: baseline ≈ V_oc of the cell
- **TPC**: baseline ≈ 0 (short-circuit)

### 3.2. Time alignment (t=0)

The instant t=0 is defined as the pulse arrival time, detected on CH1 (Q-Switch trigger):

```
1. Compute CH1 baseline (first 5%)
2. Find CH1 peak
3. Threshold = baseline + 50% × (peak − baseline)
4. t=0 = first sample where CH1 crosses this threshold
```

**Fallback**: if CH1 has no visible trigger (peak < 5σ of baseline), t=0
is set at the centre of the scope window.

### 3.3. Normalisation

```
norm(t) = ΔV(t) / ΔV_peak
```

- **TPV**: ΔV_peak is the **maximum** (signal rises from V_oc)
- **TPC**: ΔI_peak is the **point furthest from zero** (positive or negative)

Result: normalised curve in [0, 1] regardless of original amplitude,
facilitating comparison between samples and exponential fitting.

---

## 4. Saved File Structure

Each measurement generates **two files** with the same base name
(`<sample>_<TPV|TPC>_<timestamp>`):

### 4.1. `.csv` file

Plain text, easy to import in Origin/Python/etc.

**Header** (lines with `#`):

```
# Sample: <name>
# Type: TPV or TPC
# Date: <ISO datetime>
# Timebase (s/div): <value>
# N_points: <int>
# Active channel: CH3 (TPV) or CH2 (TPC)
# Baseline (V|A): <value>
# t0_idx: <pulse index>
# t0 (s): <raw pulse time>
```

**Columns**:

| Column | Description |
|---|---|
| `Time (s)` | Raw time axis (trigger at window centre) |
| `t aligned (s)` | Time axis with t=0 at pulse |
| `CH1 (V)` | Q-Switch trigger |
| `CH2 (A)` | TPC current (raw, even if measurement is TPV) |
| `CH3 (V)` | TPV voltage (raw, even if measurement is TPC) |
| `ΔCH3 (V)` or `ΔCH2 (A)` | Active channel with baseline subtracted |
| `Normalized` | Normalised signal (0 → 1) |

### 4.2. `.xlsx` file

Excel with **two sheets**:

**Sheet "Parameters"** — measurement metadata:
- Sample, Type, Date/Time
- Timebase, Time window, N points, Time resolution
- Active channel + termination
- Detected baseline
- Pulse index and time (t=0)
- Baseline window used
- t=0 detection method

**Sheet "Waveforms"** — same columns as CSV, but:
- Formatted header (bold, blue background)
- First row frozen
- Additional column `t from pulse (ms/μs)` with auto-scaled units
- Adjusted column widths

---

## 5. GUI Visualisation

The main window shows **two plots**:

### Plot 1 — "CH1 — Q-Switch Trigger"
- X axis: raw time (trigger at window centre)
- Y axis: CH1 voltage (V)
- Shows only the most recent measurement
- Used to verify that the laser trigger is being captured

### Plot 2 — "Measurement Channel — normalised, aligned at t=0"
- X axis: time from pulse (t=0 at decay onset)
- Y axis: normalised signal (ΔV / ΔV_max)
- Shows **all** history measurements (with checkboxes)
- Used to compare different samples on the same graph

---

## 6. Acquisition Settings

Configured in the **"Acquisition Settings"** panel (left side of GUI).
These are applied to the scope automatically before each measurement.

| Setting | Options | Default | Notes |
|---|---|---|---|
| **N averages** | 1 (off), 2, 4, 8, 16, 32, 64, 128, 256 | 1 (off) | Use 32–64 for perovskite |
| **Memory depth (MLEN)** | 500, 1K, 10K, 100K, 500K | 10K | More points = better time resolution |
| **Data format (DTFORM)** | BYTE, WORD | BYTE | See section 7 |
| **Settling time** | 0–60 s | 5 s | V_oc stabilisation wait |
| **Acq. timeout** | 5–120 s | 30 s | Max beam dumper open time |

**Default settings reproduce previous behaviour** (MLEN=10K, BYTE, N=1).

### Timeout guidance

The timeout must be greater than `N_avg / laser_frequency`:

| N averages | Laser @ 10 Hz | Laser @ 5 Hz |
|---|---|---|
| 32 | 3.2 s | 6.4 s |
| 64 | 6.4 s | 12.8 s |
| 128 | 12.8 s | 25.6 s |
| 256 | 25.6 s | 51.2 s |

Set timeout ≥ (N_avg / freq) + 5 s safety margin. For perovskite, keep
total exposure (settling + acquisition) under 20 s.

---

## 7. BYTE vs WORD Data Format

### Why "steps" appear in normalised curves

The visible steps in normalised TPV/TPC curves (especially for slow decays
in perovskite) are **ADC quantisation**, not random noise:

- **BYTE format** (default): signed int8, **25 codes/division**
  - 1 code = VDIV/25 volts
  - At 2 V/div: 1 code = 80 mV → ~4% of full scale → visible steps
- **WORD format**: signed int16, **6400 codes/division** (= 25 × 256)
  - 1 code = VDIV/6400 volts
  - At 2 V/div: 1 code = 0.31 mV → 256× finer than BYTE

### When WORD actually helps

WORD format only provides extra precision **with averaging**:

| Mode | BYTE precision | WORD precision |
|---|---|---|
| ACQ NORMAL (N=1) | 25 codes/div | Same — lower byte = 0 |
| ACQ AVERAGE (N≥2) | 25 codes/div (truncated) | 6400 codes/div (full 16-bit) |

**Recommendation**: use WORD only together with N averages ≥ 2.
For N=1, BYTE and WORD give identical results.

### Technical details (WaveJet 300A manual, p.17)

- WORD transfer: 2 bytes per sample (lower byte first with `DTBORD L/H`)
- Non-averaged WORD: lower byte = 0 (same information as BYTE)
- Averaged WORD: both bytes carry sub-ADC averaged data
- Conversion: `voltage = code × (VDIV / 6400.0) + OFST`

---

## 8. SCPI Protocol (WaveJet 300A)

Confirmed commands used by the software (manual 917976 Rev A):

| Command | Function |
|---|---|
| `STOP` | Freeze acquisition |
| `TRMD AUTO` | Resume continuous acquisition |
| `MLEN {500\|1K\|10K\|100K\|500K}` | Set memory depth |
| `DTFORM {BYTE\|WORD}` | Set data transfer format |
| `DTBORD L/H` | WORD byte order: lower byte first (little-endian) |
| `ACQ NORMAL` | Normal acquisition mode (N=1) |
| `ACQ AVERAGE` | Averaging mode — must precede AVGCNT |
| `AVGCNT {2,4,8,16,32,64,128,256}` | Number of averages |
| `WSGL?` | Arm SINGLE mode and **block until acquisition complete** |
| `WAVESRC {CH1\|CH2\|CH3}` | Select channel for DTWAVE? |
| `DTSTART 0` | Start address for transfer |
| `DTPOINTS N` | Number of points to transfer |
| `DTWAVE?` | Transfer waveform (IEEE 488.2 binary block) |
| `CnN:VDIV?` | Vertical scale of channel n (V/div) |
| `CnN:OFST?` | Vertical offset of channel n (V) |
| `TDIV?` | Horizontal timebase (s/div) |

`WSGL?` is the synchronisation mechanism: it sets SINGLE mode and blocks
until the scope has completed all N averages (or 1 capture for ACQ NORMAL).
No polling loop is needed — the VISA timeout provides the hard deadline.

---

## 9. Software Files

| File | Function |
|---|---|
| `main.py` | Entry point — starts the GUI |
| `gui.py` | Graphical interface (PyQt6 + pyqtgraph) |
| `hardware.py` | WaveJet (PyVISA) and Arduino (pyserial) communication |
| `core.py` | Data processing (baseline, alignment, normalisation) and saving (CSV + XLSX) |
| `diag_osc.py` | Standalone diagnostic script — tests scope communication without GUI |
| `DOCUMENTACAO.md` | This file |

---

## 10. Troubleshooting

**Software does not detect the oscilloscope**:
- Check rear USB cable (not the front USB for pendrives)
- On scope: `Utilities → Remote → Interface → USB`
- LeCroy USB driver installed? (Scope Explorer or lecroy.com)
- Run `python tpv_app/diag_osc.py` for detailed diagnostics

**Curves with very visible steps**: see section 7 (quantisation).
Use WORD format + N averages ≥ 8.

**t=0 not at decay onset**:
- Verify CH1 is connected to the laser trigger
- Check that a pulse is visible on CH1 on the scope screen
- If still failing: software uses fallback (window centre)

**Timeout during acquisition**:
- Timeout reached before N averages were completed
- Software closes beam dumper and saves whatever was accumulated
- Possible causes: laser not firing, repetition rate too low for chosen N and timeout
- Fix: increase timeout, reduce N averages, or check laser

**WORD download takes longer**:
- WORD = 2× more bytes transferred per channel
- Expected: ~2× longer download for large MLEN (e.g., 500K points)
- Mitigation: reduce MLEN or use BYTE if download time is critical

**AVGCNT error** (scope reports DDE):
- This error occurs if AVGCNT is sent before ACQ AVERAGE
- The software sends them in the correct order; this should not happen normally
- If it does: reconnect the oscilloscope and retry
