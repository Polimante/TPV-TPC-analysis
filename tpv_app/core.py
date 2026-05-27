import os
import logging
from datetime import datetime

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def reconstruir_tempo(n: int, tdiv: float) -> np.ndarray:
    """t = (i/N) * (TDIV*10) - (TDIV*5)  — trigger no centro da janela."""
    i = np.arange(n, dtype=float)
    return (i / n) * (tdiv * 10.0) - (tdiv * 5.0)


# ── Processamento: baseline, t=0, normalização ─────────────────────────────────

def subtrair_baseline(v: np.ndarray, pct: float = 0.05) -> tuple[np.ndarray, float]:
    """
    Subtrai a baseline (média dos primeiros `pct` da trace) do sinal.

    Returns (delta_v, baseline). Para TPV, baseline ≈ V_oc; para TPC ≈ 0.
    """
    n = max(1, int(len(v) * pct))
    baseline = float(np.mean(v[:n]))
    return v - baseline, baseline


def detectar_t0_ch1(ch1: np.ndarray, t: np.ndarray, pct_baseline: float = 0.05) -> int:
    """
    Detecta o instante de chegada do pulso de laser usando CH1 (Q-Switch trigger).

    Estratégia:
      1. Calcula baseline do CH1 (média dos primeiros `pct_baseline`)
      2. Calcula o pico (em valor absoluto da diferença pra baseline)
      3. Threshold = baseline + 50% × (pico - baseline)
      4. Retorna o índice da primeira amostra que cruza esse threshold

    Fallback: se CH1 não tem trigger visível (pico < 5× std da baseline),
    retorna o índice central da janela (compatível com o comportamento antigo).
    """
    n = len(ch1)
    if n == 0:
        return 0

    n_base = max(1, int(n * pct_baseline))
    base = float(np.mean(ch1[:n_base]))
    std  = float(np.std(ch1[:n_base]))

    # Pico = sample mais distante da baseline (em qualquer direção)
    deviations = ch1 - base
    i_peak = int(np.argmax(np.abs(deviations)))
    peak_dev = float(deviations[i_peak])

    # Sem trigger visível → fallback ao centro
    if abs(peak_dev) < max(5.0 * std, 1e-9):
        return n // 2

    # Threshold a 50% do pico
    threshold = base + 0.5 * peak_dev

    # Direção da borda
    if peak_dev > 0:
        crossings = np.where(ch1[:i_peak + 1] >= threshold)[0]
    else:
        crossings = np.where(ch1[:i_peak + 1] <= threshold)[0]

    if len(crossings) == 0:
        return i_peak
    return int(crossings[0])


def alinhar_tempo(t: np.ndarray, idx_t0: int) -> np.ndarray:
    """Translada o eixo de tempo para t=0 no índice `idx_t0`."""
    if len(t) == 0 or idx_t0 < 0 or idx_t0 >= len(t):
        return t.copy()
    return t - t[idx_t0]


def normalizar(v: np.ndarray, tipo: str) -> np.ndarray:
    """
    Normaliza um sinal já baseline-subtraído para o intervalo [0, 1] (TPV)
    ou [-1, 1] aproximado (TPC, se o pico for negativo).

    Para TPC, o pico é o ponto mais distante de zero (positivo ou negativo).
    Para TPV, o pico é o máximo (assume decaimento de V_oc para cima).

    Se o input ainda não foi baseline-subtraído, o resultado fica errado —
    use `subtrair_baseline` antes.
    """
    if len(v) == 0:
        return v.copy()

    if tipo == 'TPC':
        v_min = float(np.min(v))
        v_max = float(np.max(v))
        v_pico = v_min if abs(v_min) > abs(v_max) else v_max
    else:
        v_pico = float(np.max(v))

    if abs(v_pico) < 1e-12:
        return np.zeros_like(v)
    return v / v_pico


def salvar_csv(
    pasta: str,
    nome_base: str,
    tipo: str,
    dados: dict,
    tdiv: float,
    amostra: str = '',
) -> dict:
    """
    Salva waveforms em CSV + XLSX com pipeline completo:
      - baseline-subtract do canal ativo
      - alinhamento temporal usando CH1 (Q-Switch trigger)
      - normalização do ΔV / ΔI

    Retorna dict com todos os arrays processados para a GUI plotar/armazenar.
    """
    c1 = np.asarray(dados['C1'], dtype=float)
    c2 = np.asarray(dados['C2'], dtype=float)
    c3 = np.asarray(dados['C3'], dtype=float)

    n = min(len(c1), len(c2), len(c3))
    if n == 0:
        raise ValueError("Nenhum dado recebido dos canais do osciloscópio")

    c1, c2, c3 = c1[:n], c2[:n], c3[:n]
    t = reconstruir_tempo(n, tdiv)

    # Pipeline de processamento ──────────────────────────────────────────────
    canal_ativo = c3 if tipo == 'TPV' else c2
    delta_active, baseline_active = subtrair_baseline(canal_ativo)
    norm = normalizar(delta_active, tipo)

    idx_t0    = detectar_t0_ch1(c1, t)
    t_aligned = alinhar_tempo(t, idx_t0)
    t0_s      = float(t[idx_t0]) if 0 <= idx_t0 < len(t) else 0.0
    # ────────────────────────────────────────────────────────────────────────

    ts = datetime.now()
    stem = f"{nome_base}_{tipo}_{ts.strftime('%Y%m%d_%H%M%S')}"
    filepath = os.path.join(pasta, stem + '.csv')

    active_unit  = 'V' if tipo == 'TPV' else 'A'
    delta_label  = f'Δ{("CH3" if tipo == "TPV" else "CH2")} ({active_unit})'

    df = pd.DataFrame({
        'Time (s)':          t,
        't aligned (s)':     t_aligned,
        'CH1 (V)':           c1,
        'CH2 (A)':           c2,
        'CH3 (V)':           c3,
        delta_label:         delta_active,
        'Normalized':        norm,
    })

    with open(filepath, 'w', newline='', encoding='utf-8') as fh:
        fh.write(f'# Amostra: {amostra}\n')
        fh.write(f'# Tipo: {tipo}\n')
        fh.write(f'# Data: {ts.isoformat()}\n')
        fh.write(f'# Timebase (s/div): {tdiv}\n')
        fh.write(f'# N_pontos: {n}\n')
        fh.write(f'# Canal ativo: {"CH3" if tipo == "TPV" else "CH2"}\n')
        fh.write(f'# Baseline ({active_unit}): {baseline_active:.6e}\n')
        fh.write(f'# t0_idx: {idx_t0}\n')
        fh.write(f'# t0 (s): {t0_s:.6e}\n')
        df.to_csv(fh, index=False)

    logger.info("CSV salvo: %s", filepath)

    _salvar_xlsx(
        pasta, stem, tipo, amostra,
        t, t_aligned, c1, c2, c3,
        delta_active, baseline_active, norm,
        idx_t0, t0_s, tdiv, ts,
    )

    return dict(
        filepath=filepath,
        t=t,
        t_aligned=t_aligned,
        c1=c1, c2=c2, c3=c3,
        delta_active=delta_active,
        baseline_active=baseline_active,
        norm=norm,
        idx_t0=idx_t0,
        t0_s=t0_s,
    )


# ── Excel export ───────────────────────────────────────────────────────────────

def _salvar_xlsx(
    pasta: str,
    stem: str,
    tipo: str,
    amostra: str,
    t: np.ndarray,
    t_aligned: np.ndarray,
    c1: np.ndarray,
    c2: np.ndarray,
    c3: np.ndarray,
    delta_active: np.ndarray,
    baseline_active: float,
    norm: np.ndarray,
    idx_t0: int,
    t0_s: float,
    tdiv: float,
    ts: datetime,
) -> None:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        logger.warning("openpyxl not installed — Excel export skipped. Run: pip install openpyxl")
        return

    wb = Workbook()

    _build_params_sheet(
        wb.active, tipo, amostra, t, tdiv, ts,
        baseline_active=baseline_active, idx_t0=idx_t0, t0_s=t0_s,
    )
    _build_waveforms_sheet(
        wb.create_sheet('Waveforms'), tipo,
        t, t_aligned, c1, c2, c3, delta_active, norm,
    )

    filepath = os.path.join(pasta, stem + '.xlsx')
    wb.save(filepath)
    logger.info("Excel salvo: %s", filepath)


def _hdr_style():
    from openpyxl.styles import Font, PatternFill, Alignment
    font  = Font(bold=True, color='FFFFFF', size=10)
    fill  = PatternFill('solid', fgColor='1565C0')
    align = Alignment(horizontal='center', vertical='center', wrap_text=False)
    return font, fill, align


def _build_params_sheet(
    ws, tipo, amostra, t, tdiv, ts,
    baseline_active: float = 0.0,
    idx_t0: int = 0,
    t0_s: float = 0.0,
) -> None:
    from openpyxl.styles import Font, PatternFill, Alignment

    ws.title = 'Parameters'
    font, fill, align = _hdr_style()

    # Header row
    ws.append(['Parameter', 'Value'])
    for cell in ws[1]:
        cell.font  = font
        cell.fill  = fill
        cell.alignment = align

    active_ch   = 'CH3' if tipo == 'TPV' else 'CH2'
    active_unit = 'V'   if tipo == 'TPV' else 'A'
    termination = '1 MΩ  (voltage)' if tipo == 'TPV' else '50 Ω  (current / transimpedance)'
    n           = len(t)
    dt          = (tdiv * 10.0) / n if n > 0 else 0.0

    rows = [
        ('Sample',                       amostra),
        ('Measurement type',             tipo),
        ('Date / Time',                  ts.strftime('%Y-%m-%d %H:%M:%S')),
        ('Timebase  (s/div)',            f'{tdiv:.4e}'),
        ('Time window  (s)',             f'{tdiv * 10:.4e}'),
        ('N points',                     n),
        ('Time resolution  (s/point)',   f'{dt:.4e}'),
        ('Active channel',               active_ch),
        ('Active channel input',         termination),
        ('Baseline (pre-pulse mean)',    f'{baseline_active:.6e}  {active_unit}'),
        ('Pulse arrival index (CH1)',    idx_t0),
        ('Pulse arrival time (raw t)',   f'{t0_s:.6e}  s'),
        ('Baseline window',              '5%  of trace  (pre-pulse)'),
        ('t=0 detection',                'CH1 trigger  @ 50% of peak'),
    ]
    for r in rows:
        ws.append(r)

    ws.column_dimensions['A'].width = 32
    ws.column_dimensions['B'].width = 40
    ws.row_dimensions[1].height = 20


def _build_waveforms_sheet(
    ws, tipo,
    t, t_aligned, c1, c2, c3, delta_active, norm,
) -> None:
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    ws.title = 'Waveforms'
    font, fill, align = _hdr_style()

    # Auto-scale time for a human-readable column (uses aligned time for the unit choice)
    span = float(np.abs(t_aligned).max()) if len(t_aligned) > 0 else 1.0
    if span < 1e-6:
        scale, t_unit = 1e9, 'ns'
    elif span < 1e-3:
        scale, t_unit = 1e6, 'µs'
    elif span < 1.0:
        scale, t_unit = 1e3, 'ms'
    else:
        scale, t_unit = 1.0, 's'
    t_aligned_sc = t_aligned * scale

    active_ch   = 'CH3' if tipo == 'TPV' else 'CH2'
    active_unit = 'V'   if tipo == 'TPV' else 'A'

    headers = [
        'Time (s)',
        f't from pulse ({t_unit})',
        'CH1  (V)  — Q-Switch trigger',
        'CH2  (A)  — TPC current',
        'CH3  (V)  — TPV voltage',
        f'Δ{active_ch}  ({active_unit})  — baseline-subtracted',
        f'Normalized  (Δ{active_ch} / peak)',
    ]
    col_widths = [16, 18, 28, 22, 22, 32, 24]

    ws.append(headers)
    for cell in ws[1]:
        cell.font      = font
        cell.fill      = fill
        cell.alignment = align
    ws.freeze_panes = 'A2'
    ws.row_dimensions[1].height = 22

    # Data rows
    rows = [
        [float(t[i]), float(t_aligned_sc[i]),
         float(c1[i]), float(c2[i]), float(c3[i]),
         float(delta_active[i]), float(norm[i])]
        for i in range(len(t))
    ]
    for row in rows:
        ws.append(row)

    for i, w in enumerate(col_widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    # Scientific notation for Time (s) column
    sci_fmt = '0.00E+00'
    for row in ws.iter_rows(min_row=2, min_col=1, max_col=1):
        for cell in row:
            cell.number_format = sci_fmt
