import time
import logging

import numpy as np
import serial
import serial.tools.list_ports

try:
    import pyvisa
    _PYVISA_OK = True
except ImportError:
    _PYVISA_OK = False

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# OsciloscopioController  —  WaveJet 334 (300A series) via PyVISA / NI-VISA
# ══════════════════════════════════════════════════════════════════════════════
#
# Confirmed SCPI sequence (WaveJet 300A Remote Control Manual, 917976 Rev A):
#
#   -- CONFIGURATION (configurar_scope) --
#   STOP                    freeze acquisition
#   MLEN 10K                set memory depth
#   DTFORM WORD             transfer format: signed int16, 6400 codes/div
#   DTBORD L/H              little-endian byte order (lower byte first)
#   ACQ AVERAGE             averaging mode  (MUST precede AVGCNT)
#   AVGCNT 32               number of averages {2,4,8,16,32,64,128,256}
#
#   -- ARM + WAIT (aguardar_aquisicao) --
#   WSGL?                   set SINGLE mode and BLOCK until acquisition done
#                           (for ACQ AVERAGE: blocks until N averages complete)
#                           returns "+000001"
#
#   -- DOWNLOAD (baixar_waveforms) --
#   STOP                    freeze (harmless if already stopped after WSGL?)
#   WAVESRC CHn             select source channel (CH1..CH4, MATH)
#   DTSTART 0
#   DTPOINTS 10000
#   DTWAVE?                 returns #800000XXXX<payload>
#   (repeat WAVESRC + DTWAVE? for CH2, CH3)
#
#   If DTWAVE? returns a zero-byte block (#800000000) — observed on the
#   334T Touch with WORD + ACQ AVERAGE + AVGCNT≥128 — retry once with a
#   re-issued WAVESRC.
#
#   TRMD AUTO               resume acquisition
#
# BYTE scaling:  voltage = code × (vdiv / 25.0)  + ofst  (signed int8)
# WORD scaling:  voltage = code × (vdiv / 6400.0) + ofst  (signed int16, L/H)
#   WORD non-averaged: lower byte = 0 → same precision as BYTE but 2×transfer size
#   WORD averaged:     full 16-bit sub-ADC precision from accumulation
#
# DTBORD=L/H means lower byte first → NumPy dtype '<i2' (little-endian int16)

_MLEN_TO_N = {
    '500':   500,
    '1K':    1_000,
    '10K':   10_000,
    '100K':  100_000,
    '500K':  500_000,
}

_BYTES_PER_SAMPLE = {'BYTE': 1, 'WORD': 2}
_CODES_PER_DIV    = {'BYTE': 25.0, 'WORD': 6400.0}


class OsciloscopioController:
    TIMEOUT = 15_000    # ms — for waveform downloads (not acquisition wait)

    def __init__(self):
        self._rm:   pyvisa.ResourceManager | None = None
        self._inst: object | None = None
        self.port:  str | None = None
        # Acquisition config (defaults reproduce old behavior)
        self._mlen     = '10K'
        self._dtform   = 'BYTE'
        self._n_avg    = 1
        self._n_points = 10_000

    # ── Public API ─────────────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._inst is not None

    def conectar(self) -> tuple[bool, str]:
        if not _PYVISA_OK:
            return False, "pyvisa not installed — run: pip install pyvisa"
        try:
            self.desconectar()
            self._rm = pyvisa.ResourceManager()
            inst, addr = self._find_wavejet()
            if inst is None:
                return False, (
                    "WaveJet not found via NI-VISA.\n"
                    "Check: rear USB cable, LeCroy USB driver installed,\n"
                    "and on the scope: Utilities → Remote → Interface → USB."
                )
            self._inst = inst
            self.port  = addr
            logger.info("WaveJet connected: %s", addr)
            return True, addr
        except Exception as exc:
            logger.error("Connection failed: %s", exc)
            return False, str(exc)

    def desconectar(self):
        if self._inst:
            try:
                self._inst.close()
            except Exception:
                pass
            self._inst = None
        if self._rm:
            try:
                self._rm.close()
            except Exception:
                pass
            self._rm = None
        self.port = None

    def configurar_scope(self, n_avg: int, mlen: str, dtform: str) -> None:
        """
        Configure acquisition parameters — does NOT arm or start acquisition.

        Call this before opening the beam dumper. Sends:
          STOP → MLEN → DTFORM → [DTBORD if WORD] → ACQ [AVERAGE+AVGCNT | NORMAL]

        n_avg=1 maps to ACQ NORMAL (averaging off).
        AVGCNT must follow ACQ AVERAGE per WaveJet 300A manual (p.18).
        """
        if not self._inst:
            raise RuntimeError("Oscilloscope not connected")
        inst = self._inst

        _cmd(inst, 'STOP')
        time.sleep(0.2)

        _cmd(inst, f'MLEN {mlen}')
        _cmd(inst, f'DTFORM {dtform}')
        if dtform == 'WORD':
            _cmd(inst, 'DTBORD L/H')   # little-endian: lower byte first
        time.sleep(0.1)

        if n_avg > 1:
            _cmd(inst, 'ACQ AVERAGE')
            time.sleep(0.05)
            _cmd(inst, f'AVGCNT {n_avg}')
        else:
            _cmd(inst, 'ACQ NORMAL')
        time.sleep(0.1)

        # Confirm actual MLEN the scope accepted (it may round)
        mlen_resp = _qry(inst, 'MLEN?').strip()
        self._n_points = _MLEN_TO_N.get(mlen_resp, _MLEN_TO_N.get(mlen, 10_000))
        self._mlen   = mlen
        self._dtform = dtform
        self._n_avg  = n_avg

        logger.info("Scope configured: MLEN=%s (%d pts)  DTFORM=%s  AVG=%d",
                    mlen, self._n_points, dtform, n_avg)

    def aguardar_aquisicao(self, timeout_s: float) -> bool:
        """
        Arm scope (SINGLE mode) and block until acquisition complete or timeout.

        Sends WSGL? which:
          - Sets trigger mode to SINGLE
          - Blocks until the single measurement is done (= N averages for ACQ AVERAGE)
          - Returns "+000001" on success

        VISA timeout is temporarily set to timeout_s + 5 s buffer.
        Returns True if acquisition completed, False on timeout/error.
        """
        if not self._inst:
            raise RuntimeError("Oscilloscope not connected")

        old_timeout = self._inst.timeout
        self._inst.timeout = int(timeout_s * 1000) + 5_000
        try:
            _qry(self._inst, 'WSGL?')
            logger.info("WSGL? returned — acquisition complete")
            return True
        except Exception as exc:
            logger.warning("WSGL? did not complete within timeout: %s", exc)
            try:
                _cmd(self._inst, 'STOP')
            except Exception:
                pass
            return False
        finally:
            self._inst.timeout = old_timeout

    def baixar_waveforms(self) -> dict:
        """
        Freeze scope, download CH1/CH2/CH3, resume scope.

        Uses the DTFORM and memory depth set by the most recent configurar_scope()
        call (defaults: BYTE, 10K).

        Returns:
            {'C1': [float, ...], 'C2': [...], 'C3': [...], 'TDIV': float}
        """
        if not self._inst:
            raise RuntimeError("Oscilloscope not connected")

        inst   = self._inst
        n      = self._n_points
        dtform = self._dtform

        _cmd(inst, 'STOP')
        time.sleep(0.2)

        tdiv = float(_qry(inst, 'TDIV?'))
        logger.debug("Downloading: MLEN=%s (%d pts)  DTFORM=%s  TDIV=%.3e s/div",
                     self._mlen, n, dtform, tdiv)

        result: dict = {}
        for wavesrc, chn in [('CH1', 'C1'), ('CH2', 'C2'), ('CH3', 'C3')]:
            vdiv = float(_qry(inst, f'{chn}:VDIV?'))
            ofst = float(_qry(inst, f'{chn}:OFST?'))
            raw  = _download_wave(inst, wavesrc, n, dtform)
            _drain_buffer(inst)
            v    = _codes_to_volts(raw, vdiv, ofst, dtform)
            result[chn] = v.tolist()
            if v.size > 0:
                logger.debug("%s: %d pts  range [%.3g, %.3g] V",
                             wavesrc, len(v), v.min(), v.max())
            else:
                logger.warning("%s: scope returned 0 points", wavesrc)

        result['TDIV'] = tdiv
        _cmd(inst, 'TRMD AUTO')
        return result

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _find_wavejet(self):
        """Scan all VISA resources and return (inst, addr) for the WaveJet."""
        for r in self._rm.list_resources('?*'):
            inst = None
            try:
                inst = self._rm.open_resource(r)
                inst.timeout = 5_000
                inst.write_termination = '\n'
                inst.read_termination  = '\n'
                if r.startswith('ASRL'):
                    inst.baud_rate = 9_600
                    time.sleep(0.3)
                    try:
                        inst.clear()
                    except Exception:
                        pass
                    time.sleep(0.1)
                idn = inst.query('*IDN?').strip()
                logger.debug("VISA probe %s: %s", r, idn[:60])
                if 'LECROY' in idn.upper():
                    inst.timeout = self.TIMEOUT
                    logger.info("WaveJet found at %s: %s", r, idn)
                    return inst, r
                inst.close()
            except Exception as exc:
                logger.debug("VISA probe %s: %s", r, exc)
                if inst:
                    try:
                        inst.close()
                    except Exception:
                        pass
        return None, None


# ══════════════════════════════════════════════════════════════════════════════
# Module-level helpers  (used by both OsciloscopioController and diag_osc.py)
# ══════════════════════════════════════════════════════════════════════════════

def _cmd(inst, cmd: str) -> None:
    inst.write(cmd)
    time.sleep(0.05)


def _qry(inst, cmd: str) -> str:
    resp = inst.query(cmd).strip()
    if not resp:
        time.sleep(0.15)
        resp = inst.query(cmd).strip()
    return resp


def _drain_buffer(inst) -> None:
    """Discard trailing delimiter bytes the WaveJet appends after binary blocks."""
    if not inst.resource_name.startswith('ASRL'):
        return
    time.sleep(0.1)
    try:
        n = inst.bytes_in_buffer
        if n > 0:
            inst.read_bytes(n, break_on_termchar=False)
    except Exception:
        pass


def _download_wave(inst, wavesrc: str, n_points: int,
                   dtform: str = 'BYTE') -> bytes:
    """
    Download waveform for the given channel in the given DTFORM.

    Uses the documented WaveJet 300A sequence: WAVESRC CHn → DTSTART 0 →
    DTPOINTS n → DTWAVE? (no argument). The channel is selected via WAVESRC.

    If the scope replies with a zero-byte block (#800000000) — sometimes seen
    on the 334T Touch with WORD + ACQ AVERAGE + high AVGCNT — we retry once
    after a short pause and a fresh WAVESRC.

    For WORD format: expected_bytes = 2 × n_points (2 bytes per sample).
    For BYTE format: expected_bytes = n_points.
    """
    expected_bytes = n_points * _BYTES_PER_SAMPLE[dtform]

    for attempt in range(2):
        _cmd(inst, f'WAVESRC {wavesrc}')
        _cmd(inst, 'DTSTART 0')
        _cmd(inst, f'DTPOINTS {n_points}')
        time.sleep(0.15)
        inst.write('DTWAVE?')
        time.sleep(0.3)

        if inst.resource_name.startswith('ASRL'):
            payload = _read_asrl_block(inst, expected_bytes)
        else:
            raw = inst.read_raw()
            payload = _parse_ieee_block(raw, expected_bytes)

        if len(payload) > 0:
            return payload

        if attempt == 0:
            logger.warning(
                "%s returned 0 bytes — re-issuing WAVESRC and retrying", wavesrc
            )
            _drain_buffer(inst)
            time.sleep(0.5)

    return b''


def _read_asrl_block(inst, expected_bytes: int) -> bytes:
    """
    Read an IEEE 488.2 binary block from a COM-port (ASRL) resource.

    Reads the header byte-by-byte (skipping leading \\r\\n) until the byte
    count is known, then reads exactly the announced number of payload bytes.
    This avoids waiting on bytes that never arrive when the scope responds
    with a zero-byte payload (e.g. '\\r#800000000\\n').

    expected_bytes: total payload bytes (n_points for BYTE, 2×n_points for WORD).
    """
    prefix = b''
    for _ in range(8):
        b = inst.read_bytes(1, break_on_termchar=False)
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

    n_digits = int(inst.read_bytes(1, break_on_termchar=False))
    n_bytes  = int(inst.read_bytes(n_digits, break_on_termchar=False))

    if n_bytes == 0:
        logger.warning("Scope returned empty IEEE block (0 bytes payload)")
        return b''

    payload = inst.read_bytes(n_bytes, break_on_termchar=False)
    if len(payload) < n_bytes:
        raise RuntimeError(
            f"Truncated block: expected {n_bytes} bytes, received {len(payload)}.\n"
            "Try increasing TIMEOUT or reducing N_POINTS / MLEN."
        )
    if n_bytes != expected_bytes:
        logger.warning("Scope returned %d bytes, expected %d", n_bytes, expected_bytes)
    return payload


def _parse_ieee_block(raw: bytes, expected_bytes: int) -> bytes:
    """
    Parse IEEE 488.2 definite-length block: #8<8 ASCII digits><payload>

    WaveJet 300A always uses #8 (8-digit byte count).
    Leading CR/LF bytes (scope command echo) are stripped automatically.

    expected_bytes: total payload bytes (n_points for BYTE, 2×n_points for WORD).
    """
    raw = raw.lstrip(b'\r\n ')
    if len(raw) < 12:
        raise RuntimeError(
            f"Response too short ({len(raw)} bytes). "
            "Check USB cable and that MLEN/DTPOINTS were accepted.\n"
            f"Raw (first 60 bytes): {raw[:60]!r}"
        )
    if raw[0:1] != b'#':
        raise RuntimeError(
            f"Expected IEEE block starting with '#', got: {raw[:60]!r}\n"
            "Possible cause: scope sent an error message instead of data."
        )
    n_digits = int(raw[1:2])
    n_bytes  = int(raw[2 : 2 + n_digits])
    payload  = raw[2 + n_digits : 2 + n_digits + n_bytes]

    if len(payload) < n_bytes:
        raise RuntimeError(
            f"Truncated block: expected {n_bytes} bytes, received {len(payload)}.\n"
            "Try increasing TIMEOUT or reducing N_POINTS / MLEN."
        )
    if n_bytes != expected_bytes:
        logger.warning("Scope returned %d bytes, expected %d", n_bytes, expected_bytes)
    return payload


def _codes_to_volts(raw_bytes: bytes, vdiv: float, ofst: float,
                    dtform: str = 'BYTE') -> np.ndarray:
    """
    Convert ADC codes to volts.

    BYTE (signed int8, 25 codes/div):
        voltage = code × (vdiv / 25.0) + ofst

    WORD (signed int16 little-endian, DTBORD=L/H, 6400 codes/div):
        voltage = code × (vdiv / 6400.0) + ofst

    WORD non-averaged: lower byte = 0, so code = Ui×256; result same as BYTE.
    WORD averaged:     full 16-bit sub-ADC precision from accumulation.
    Scale factor 6400 = 25 × 256 in both cases (manual p.17).
    """
    if dtform == 'WORD':
        codes = np.frombuffer(raw_bytes, dtype='<i2').astype(np.float64)
        return codes * (vdiv / 6400.0) + ofst
    else:
        codes = np.frombuffer(raw_bytes, dtype=np.int8).astype(np.float64)
        return codes * (vdiv / 25.0) + ofst


# ══════════════════════════════════════════════════════════════════════════════
# ArduinoController  —  beam dumper servo via pyserial
# ══════════════════════════════════════════════════════════════════════════════

class ArduinoController:
    BAUD = 9600
    VENDOR_IDS = {0x2341, 0x1A86}
    KEYWORDS = ('arduino', 'ch340', 'atmega')

    def __init__(self):
        self.ser: serial.Serial | None = None
        self.port: str | None = None

    def detectar_porta(self) -> str | None:
        for info in serial.tools.list_ports.comports():
            vid  = info.vid or 0
            desc = (info.description or '').lower()
            mfg  = (info.manufacturer or '').lower()
            if vid in self.VENDOR_IDS:
                return info.device
            if any(k in desc or k in mfg for k in self.KEYWORDS):
                return info.device
        return None

    def conectar(self) -> tuple[bool, str]:
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
            port = self.detectar_porta()
            if port is None:
                return False, "Arduino not found"
            self.ser = serial.Serial(port, self.BAUD, timeout=2)
            self.port = port
            time.sleep(2)
            logger.info("Arduino connected at %s", port)
            return True, port
        except Exception as exc:
            logger.error("Arduino connection failed: %s", exc)
            return False, str(exc)

    def desconectar(self):
        if self.ser and self.ser.is_open:
            try:
                self._send('C')
            except Exception:
                pass
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        self.port = None

    def _send(self, cmd: str):
        if self.ser and self.ser.is_open:
            self.ser.write(cmd.encode('ascii'))
            self.ser.flush()

    def abrir(self):
        self._send('O')
        logger.info("Beam dumper: OPEN")

    def fechar(self):
        self._send('C')
        logger.info("Beam dumper: CLOSED")
