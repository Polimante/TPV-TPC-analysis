# -*- coding: utf-8 -*-
"""
diag_osc.py  -  WaveJet 334 communication diagnostic
=====================================================
Run this BEFORE using the GUI to verify that the USB link and SCPI
protocol are working correctly.

What it does:
  1. Lists every VISA resource NI-VISA can see
  2. Finds the WaveJet 334 (tries USB and COM ports)
  3. Downloads CH1, CH2, CH3 with the correct SCPI sequence
  4. Saves a CSV and a PNG plot to  tpv_app/diag_output/

Compare the saved CSV with a waveform exported via USB pendrive from the
scope — the voltage values should match within the ADC noise floor (~1 code
= vdiv/25 volts).

Usage:
    python tpv_app/diag_osc.py
"""
import sys
import time
import struct
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

try:
    import pyvisa
except ImportError:
    sys.exit("ERROR: pyvisa not installed.  Run:  pip install pyvisa")

# ── Settings ──────────────────────────────────────────────────────────────────
MLEN      = '10K'     # memory depth: 500 | 1K | 10K | 100K | 500K
N_POINTS  = 10_000    # must match MLEN numeric value
TIMEOUT   = 15_000    # ms — 15 s covers USB transfer of 10K points with margin
OUT_DIR   = Path(__file__).parent / 'diag_output'

MLEN_TO_N = {'500': 500, '1K': 1_000, '10K': 10_000,
             '100K': 100_000, '500K': 500_000}
# ─────────────────────────────────────────────────────────────────────────────


def main():
    OUT_DIR.mkdir(exist_ok=True)
    rm = pyvisa.ResourceManager()

    _print_resources(rm)

    scope = _find_wavejet(rm)
    if scope is None:
        print(
            "\nERROR: WaveJet not found.\n"
            "  Checklist:\n"
            "    1. Rear USB-B cable connected to PC?\n"
            "    2. Teledyne LeCroy USB driver installed?\n"
            "       (install Scope Explorer from the CD or lecroy.com)\n"
            "    3. On the scope: Utilities > Remote > Interface > USB\n"
            "    4. NI MAX (NI-VISA) shows the instrument?"
        )
        rm.close()
        sys.exit(1)

    try:
        _run(scope)
    except Exception as exc:
        print(f"\nERROR during acquisition: {exc}")
        raise
    finally:
        try:
            scope.write('TRMD AUTO')   # always restore scope to running
        except Exception:
            pass
        scope.close()
        rm.close()


# ── Resource discovery ────────────────────────────────────────────────────────

def _print_resources(rm: pyvisa.ResourceManager) -> None:
    print("=== VISA resources visible to NI-VISA ===")
    resources = rm.list_resources('?*')
    if not resources:
        print("  (none -- is NI-VISA installed and the USB driver loaded?)")
    for r in resources:
        print(f"  {r}")
    print()


def _find_wavejet(rm: pyvisa.ResourceManager):
    """
    Scan all VISA resources (USB-TMC and COM ports).
    Prints what each port responds (or the error) so failures are visible.
    Returns the first resource that responds to *IDN? with 'LECROY'.
    """
    print("=== Probing each resource for *IDN? ===")
    for r in rm.list_resources('?*'):
        inst = None
        try:
            inst = rm.open_resource(r)
            inst.timeout = 5_000          # 5 s -- scope may be slow after idle
            inst.write_termination = '\n'
            inst.read_termination  = '\n'

            if r.startswith('ASRL'):
                inst.baud_rate = 9_600
                time.sleep(0.3)           # let COM port settle after open
                # clear stale bytes left from previous session
                try:
                    inst.clear()
                except Exception:
                    pass
                time.sleep(0.1)

            idn = inst.query('*IDN?').strip()
            print(f"  {r:25s} -> {idn[:60]}")

            if 'LECROY' in idn.upper():
                print(f"\nFound WaveJet at: {r}")
                inst.timeout = TIMEOUT
                return inst
            inst.close()

        except Exception as exc:
            print(f"  {r:25s} -> ERROR: {exc}")
            if inst:
                try:
                    inst.close()
                except Exception:
                    pass
    print()
    return None


# ── Acquisition helpers ───────────────────────────────────────────────────────

def _cmd(scope, cmd: str) -> None:
    scope.write(cmd)
    time.sleep(0.05)


def _qry(scope, cmd: str) -> str:
    resp = scope.query(cmd).strip()
    if not resp:
        # Stale delimiter byte consumed the first read — retry once
        time.sleep(0.15)
        resp = scope.query(cmd).strip()
    return resp


def _drain_buffer(scope) -> None:
    """
    Discard bytes left in the COM-port buffer after a binary block.
    The WaveJet appends LF+CR after every response; those bytes must be
    consumed before the next text query or the query returns an empty string.
    """
    if not scope.resource_name.startswith('ASRL'):
        return
    time.sleep(0.1)      # give trailing bytes time to arrive
    try:
        n = scope.bytes_in_buffer
        if n > 0:
            scope.read_bytes(n, break_on_termchar=False)
    except Exception:
        pass


def _download_wave(scope, wavesrc: str, n_points: int) -> bytes:
    """
    Select channel with WAVESRC and download DTFORM BYTE waveform.
    Returns raw bytes (signed int8 ADC codes).

    Handles both USB-TMC resources (read_raw + USB EOM) and ASRL/COM-port
    resources (read_bytes with break_on_termchar=False to avoid the binary
    data being cut at any 0x0A byte that the termchar machinery would mistake
    for end-of-message).
    """
    _cmd(scope, f'WAVESRC {wavesrc}')
    _cmd(scope, 'DTSTART 0')
    _cmd(scope, f'DTPOINTS {n_points}')
    time.sleep(0.15)
    scope.write('DTWAVE?')
    time.sleep(0.3)

    if scope.resource_name.startswith('ASRL'):
        return _read_asrl_block(scope, n_points)
    else:
        raw = scope.read_raw()
        return _parse_ieee_block(raw, n_points)


def _read_asrl_block(scope, n_points: int) -> bytes:
    """
    Read an IEEE 488.2 binary block from a COM-port (ASRL) resource.

    Reads the header byte-by-byte (skipping leading \\r\\n) until the byte
    count is known, then reads exactly the announced payload. This avoids
    blocking on bytes that never arrive when the scope responds with
    '\\r#800000000\\n' (zero-byte payload).
    """
    prefix = b''
    for _ in range(8):
        b = scope.read_bytes(1, break_on_termchar=False)
        if b == b'#':
            break
        if b in (b'\r', b'\n', b' '):
            prefix += b
            continue
        raise RuntimeError(
            f"Expected '#' at start of IEEE block, got {b!r} "
            f"(after prefix {prefix!r}). Scope likely returned an error message."
        )
    else:
        raise RuntimeError(
            f"No '#' marker after {len(prefix)} bytes: {prefix!r}"
        )

    n_digits = int(scope.read_bytes(1, break_on_termchar=False))
    n_bytes  = int(scope.read_bytes(n_digits, break_on_termchar=False))

    if n_bytes == 0:
        print(f"    WARNING: scope returned 0 bytes of payload")
        return b''

    payload = scope.read_bytes(n_bytes, break_on_termchar=False)
    if len(payload) < n_bytes:
        raise RuntimeError(
            f"Truncated block: expected {n_bytes} bytes, received {len(payload)}.\n"
            "Try increasing TIMEOUT or reducing N_POINTS / MLEN."
        )
    return payload


def _parse_ieee_block(raw: bytes, expected_n: int) -> bytes:
    """
    Parse IEEE 488.2 definite-length block: #8<8 ASCII digits><payload>
    The WaveJet always uses #8 (8 digits for the byte count).
    Leading CR/LF bytes (scope command echo) are stripped automatically.
    """
    raw = raw.lstrip(b'\r\n ')          # scope sometimes prefixes responses
    if len(raw) < 12:
        raise RuntimeError(
            f"Response too short ({len(raw)} bytes). "
            "Check USB connection and that MLEN/DTPOINTS were accepted."
        )
    if raw[0:1] != b'#':
        printable = raw[:60].decode('ascii', errors='replace')
        raise RuntimeError(
            f"Expected IEEE block starting with '#', got: {printable!r}"
        )
    n_digits = int(raw[1:2])
    n_bytes  = int(raw[2 : 2 + n_digits])
    payload  = raw[2 + n_digits : 2 + n_digits + n_bytes]

    if len(payload) < n_bytes:
        raise RuntimeError(
            f"Truncated block: expected {n_bytes} bytes, received {len(payload)}.\n"
            "Increase TIMEOUT or reduce N_POINTS."
        )
    if n_bytes != expected_n:
        print(f"  WARNING: scope returned {n_bytes} bytes, expected {expected_n}.")
    return payload


def _codes_to_volts(raw_bytes: bytes, vdiv: float, ofst: float) -> np.ndarray:
    """
    Convert BYTE-format ADC codes to volts.

    WaveJet 300A BYTE format: signed int8, 25 codes per division.
    Code 0 = screen centre.  voltage = code × (vdiv / 25) + ofst
    """
    codes = np.frombuffer(raw_bytes, dtype=np.int8).astype(np.float64)
    return codes * (vdiv / 25.0) + ofst


def _make_time(n: int, tdiv: float) -> np.ndarray:
    """Time axis: trigger at centre.  t ∈ [-5·tdiv, +5·tdiv)."""
    return np.linspace(-5.0 * tdiv, 5.0 * tdiv, n, endpoint=False)


# ── Main acquisition run ──────────────────────────────────────────────────────

def _run(scope) -> None:
    ts = datetime.now()
    print(f"\n=== Starting diagnostic at {ts.strftime('%H:%M:%S')} ===\n")

    # Freeze acquisition so we get a stable waveform
    _cmd(scope, 'STOP')
    time.sleep(0.4)

    # Set memory depth and transfer format
    _cmd(scope, f'MLEN {MLEN}')
    _cmd(scope, 'DTFORM BYTE')
    time.sleep(0.1)

    # Read actual memory depth (scope might round our request)
    mlen_str = _qry(scope, 'MLEN?')
    n_actual  = MLEN_TO_N.get(mlen_str, N_POINTS)
    tdiv      = float(_qry(scope, 'TDIV?'))
    print(f"  MLEN = {mlen_str} ({n_actual} pts)  |  TDIV = {tdiv:.3e} s/div")

    t = _make_time(n_actual, tdiv)
    waveforms = {'Time (s)': t}
    meta      = {}

    # Download all three channels
    for wavesrc, chn in [('CH1', 'C1'), ('CH2', 'C2'), ('CH3', 'C3')]:
        print(f"\n  [{wavesrc}]")
        vdiv = float(_qry(scope, f'{chn}:VDIV?'))
        ofst = float(_qry(scope, f'{chn}:OFST?'))
        cpl  = _qry(scope, f'{chn}:CPL?')
        print(f"    VDIV = {vdiv:.4g} V/div  |  OFST = {ofst:.4g} V  |  CPL = {cpl}")

        raw = _download_wave(scope, wavesrc, n_actual)
        print(f"    Received {len(raw)} bytes")
        _drain_buffer(scope)

        v = _codes_to_volts(raw, vdiv, ofst)
        waveforms[f'{wavesrc} (V)'] = v
        meta[wavesrc] = dict(vdiv=vdiv, ofst=ofst, cpl=cpl)

        vmin, vmax = v.min(), v.max()
        print(f"    Voltage: [{vmin:.4g} V, {vmax:.4g} V]  "
              f"mean = {v.mean():.4g} V  std = {v.std():.4g} V")
        if abs(vmax - vmin) < (vdiv * 0.1):
            print(f"    NOTE: very flat trace — is CH{wavesrc[-1]} connected?")

    # Restore scope to running
    _cmd(scope, 'TRMD AUTO')
    print("\n  Scope returned to AUTO mode.")

    # Save CSV
    df       = pd.DataFrame(waveforms)
    stem     = f"diag_{ts.strftime('%Y%m%d_%H%M%S')}"
    csv_path = OUT_DIR / f"{stem}.csv"
    with open(csv_path, 'w', newline='', encoding='utf-8') as fh:
        fh.write(f"# WaveJet 334 diagnostic  {ts.isoformat()}\n")
        fh.write(f"# TDIV={tdiv:.3e} s/div  MLEN={mlen_str}\n")
        for ch, m in meta.items():
            fh.write(f"# {ch}: VDIV={m['vdiv']:.4g} V/div  "
                     f"OFST={m['ofst']:.4g} V  CPL={m['cpl']}\n")
        df.to_csv(fh, index=False)
    print(f"\n  CSV saved:  {csv_path}")

    # Save PNG
    _save_png(waveforms, meta, t, tdiv, stem, ts)


def _save_png(waveforms, meta, t, tdiv, stem, ts) -> None:
    channels = [k for k in waveforms if k != 'Time (s)']
    colors   = ['#1f77b4', '#ff7f0e', '#2ca02c']

    # Auto-scale time axis
    span = abs(t[-1] - t[0])
    if span < 1e-6:
        t_plot, t_unit = t * 1e9, 'ns'
    elif span < 1e-3:
        t_plot, t_unit = t * 1e6, 'us'
    elif span < 1.0:
        t_plot, t_unit = t * 1e3, 'ms'
    else:
        t_plot, t_unit = t, 's'

    fig, axes = plt.subplots(len(channels), 1,
                             figsize=(12, 3 * len(channels)),
                             sharex=True)
    if len(channels) == 1:
        axes = [axes]

    for ax, col, color in zip(axes, channels, colors):
        ax.plot(t_plot, waveforms[col], lw=0.7, color=color)
        ax.set_ylabel(col, fontsize=10)
        ax.grid(True, alpha=0.25)
        ch_key = col.split(' ')[0]          # e.g. 'CH1'
        if ch_key in meta:
            vd = meta[ch_key]['vdiv']
            ax.set_title(f"{col}  ({vd:.3g} V/div)", fontsize=9, loc='right', pad=2)

    axes[-1].set_xlabel(f'Time ({t_unit})', fontsize=10)
    axes[0].set_title(
        f"WaveJet 334 diagnostic — {ts.strftime('%Y-%m-%d %H:%M:%S')}",
        fontsize=11,
    )
    fig.tight_layout()

    png_path = OUT_DIR / f"{stem}.png"
    fig.savefig(png_path, dpi=120)
    plt.close(fig)
    print(f"  PNG saved:  {png_path}")
    print(f"\n  Compare {stem}.csv with a CSV exported from the scope's USB pendrive.")
    print("  Voltage values should match within ~1 ADC code (= VDIV/25 volts).\n")


if __name__ == '__main__':
    main()
