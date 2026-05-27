"""
TPV — Script de medida automatizada
====================================
Equipamentos controlados:
  - Osciloscópio Teledyne LeCroy WaveJet Touch 334T (COM port serial)
  - Arduino Mega 2560 (beam dumper servo, serial USB)
  - Cornerstone 260 (monocromador/shutter, serial USB) — implementação futura

Fluxo por intensidade de luz:
  1. Abre shutter do monocromador (luz de bias)
  2. Aguarda estabilização iônica (configurável)
  3. Configura osciloscópio para TPV
  4. Abre beam dumper (deixa laser passar)
  5. Arma trigger single no osciloscópio
  6. Aguarda aquisição completar (laser dispara a ~30 Hz)
  7. Fecha beam dumper (bloqueia laser)
  8. Baixa waveform do CH2
  9. Salva CSV
  10. Repete para próxima intensidade

Autor: Lucas (UFABC)
Data: 2026
"""

import serial
import serial.tools.list_ports
import time
import os
import numpy as np
import pandas as pd
from scipy.optimize import curve_fit
from datetime import datetime

# ══════════════════════════════════════════════════════════════════════════════
# PARÂMETROS DO EXPERIMENTO — edite aqui antes de cada sessão
# ══════════════════════════════════════════════════════════════════════════════

# Identificação da amostra
AMOSTRA = "PSC_001"           # nome da amostra — usado no nome dos arquivos

# Intensidades de luz de bias (em % do máximo do monocromador, 0–100)
# Ajuste conforme a faixa que quiser varrer
INTENSIDADES = [100]

# Tempo de estabilização iônica após cada mudança de intensidade (segundos)
# Literatura recomenda >= 2 s; para perovskitas FA/MA usar >= 5 s
TEMPO_ESTABILIZACAO = 5.0

# Timeout máximo aguardando o trigger do laser (segundos)
# Laser a 30 Hz: um pulso a cada ~33 ms; 10 s é mais que suficiente
TIMEOUT_TRIGGER = 10.0

# Pasta onde salvar os dados
PASTA_SAIDA = r"C:\Users\lucas\OneDrive - Universidade Federal do ABC\Área de Trabalho\Teledyne\testes"

# Configurações do osciloscópio
OSC_TDIV    = "50E-6"   # tempo/div (s) — 50 µs/div dá janela de 500 µs
OSC_VDIV    = "20E-3"   # tensão/div CH2 (V) — 20 mV/div para TPV small-signal
OSC_CPL_CH1 = "DC50"    # acoplamento CH1 (trigger Q-switch): DC 50 Ω
OSC_CPL_CH2 = "DC1M"    # acoplamento CH2 (célula TPV): DC 1 MΩ
OSC_AVGCNT  = 128       # número de médias

# Posições do servo do beam dumper (em graus)
# Ajuste conforme a posição física do seu setup
SERVO_ABERTO  = 180     # feixe livre — laser passa
SERVO_FECHADO = 90      # beam dumper na frente — laser bloqueado

# ══════════════════════════════════════════════════════════════════════════════
# FUNÇÕES DE CONEXÃO
# ══════════════════════════════════════════════════════════════════════════════

def conectar_osciloscopio(porta='COM4', baudrate=9600):
    """Conecta ao osciloscópio WaveJet via COM port virtual."""
    print(f"Conectando ao osciloscópio em {porta}...")
    scope = serial.Serial(porta, baudrate, timeout=5)
    time.sleep(0.5)

    # Verifica identidade
    idn = _cmd(scope, '*IDN?')
    if 'LECROY' not in idn.upper():
        raise RuntimeError(f"Instrumento inesperado: {idn}")
    print(f"  Osciloscópio: {idn}")
    return scope


def conectar_arduino():
    """
    Encontra e conecta ao Arduino Mega automaticamente
    baseado no VID/descrição do dispositivo.
    """
    print("Procurando Arduino...")
    portas = serial.tools.list_ports.comports()

    for porta in portas:
        desc = (porta.description or '').upper()
        vid  = str(porta.vid or '')
        if any([
            'ARDUINO' in desc,
            'CH340'   in desc,
            'ATMEGA'  in desc,
            '2341'    in vid,   # VID oficial Arduino
            '1A86'    in vid,   # VID chip CH340
        ]):
            print(f"  Arduino encontrado: {porta.device} — {porta.description}")
            conn = serial.Serial(porta.device, 9600, timeout=2)
            time.sleep(2)       # aguarda inicialização do Arduino
            return conn

    raise RuntimeError(
        "Arduino não encontrado. Verifique o cabo USB e o driver."
    )


# ══════════════════════════════════════════════════════════════════════════════
# FUNÇÕES DE CONTROLE DO OSCILOSCÓPIO
# ══════════════════════════════════════════════════════════════════════════════

def _cmd(scope, comando, aguarda=0.5):
    """Envia comando SCPI e lê resposta se for query."""
    scope.reset_input_buffer()
    scope.write((comando + '\n').encode())
    time.sleep(aguarda)
    if '?' in comando:
        resp = scope.read(scope.in_waiting or 1)
        return resp.decode('latin-1').strip()
    return None


def configurar_osciloscopio(scope):
    """Aplica todas as configurações necessárias para medida TPV."""
    print("Configurando osciloscópio...")

    _cmd(scope, f'TDIV {OSC_TDIV}')          # timebase
    _cmd(scope, f'C1:CPL {OSC_CPL_CH1}')     # CH1: DC 50Ω (Q-switch sync)
    _cmd(scope, f'C1:VDIV 2')                # CH1: 2 V/div
    _cmd(scope, f'C2:CPL {OSC_CPL_CH2}')     # CH2: DC 1MΩ (célula)
    _cmd(scope, f'C2:VDIV {OSC_VDIV}')       # CH2: 20 mV/div
    _cmd(scope, 'ACQ AVERAGE')               # modo average
    _cmd(scope, f'AVGCNT {OSC_AVGCNT}')      # número de médias
    _cmd(scope, 'DTFORM ASCII')              # formato de dados ASCII

    # Confirma configurações
    print(f"  Timebase : {_cmd(scope, 'TDIV?')}")
    print(f"  CH1 CPL  : {_cmd(scope, 'C1:CPL?')}")
    print(f"  CH2 CPL  : {_cmd(scope, 'C2:CPL?')}")
    print(f"  CH2 Vdiv : {_cmd(scope, 'C2:VDIV?')}")
    print(f"  Acq mode : {_cmd(scope, 'ACQ?')}")
    print(f"  Avg count: {_cmd(scope, 'AVGCNT?')}")
    print("  Configuração OK.")


def armar_trigger(scope):
    """
    Força reset completo do estado de trigger antes de armar Single.
    """
    _cmd(scope, 'TRMD STOP',   aguarda=0.3)   # para tudo
    _cmd(scope, 'CLSW',        aguarda=0.3)   # clear sweep — limpa aquisições anteriores
    _cmd(scope, 'TRMD AUTO',   aguarda=0.3)   # volta para auto
    time.sleep(0.3)
    _cmd(scope, 'TRMD SINGLE', aguarda=0.3)   # arma single limpo


def aguardar_aquisicao(scope, timeout=TIMEOUT_TRIGGER):
    # Lê sweeps iniciais como referência
    sweeps_ini = _cmd(scope, 'ACQC?', aguarda=0.3)
    print(f"    Sweeps iniciais: {sweeps_ini}")

    t0 = time.time()
    while time.time() - t0 < timeout:
        scope.reset_input_buffer()
        scope.write(b'TRMD?\n')
        time.sleep(0.15)
        resp = scope.read(scope.in_waiting or 1)
        modo = resp.decode('latin-1').strip() if resp else ''

        sweeps = _cmd(scope, 'ACQC?', aguarda=0.2)
        print(f"    TRMD={modo} | ACQC={sweeps}")

        if any(x in modo.upper() for x in ['STOP', 'TRIG']):
            return True
        # Se o número de sweeps aumentou, o trigger disparou
        try:
            if int(sweeps) > int(sweeps_ini):
                return True
        except:
            pass

    return False


def baixar_waveform(scope):
    """
    Baixa a waveform do CH2 e retorna arrays de tempo e tensão.
    Retorna (t_array, V_array) em segundos e volts.
    """
    # Lê informações da waveform para reconstruir o eixo de tempo
    info = _cmd(scope, 'DTINF? CH2', aguarda=1.5)

    # Baixa os dados
    scope.reset_input_buffer()
    scope.write(b'DTWAVE? CH2\n')

    # Lê em loop até timeout — baudrate 9600 é lento para dados grandes
    dados = b''
    t0 = time.time()
    while time.time() - t0 < 10.0:
        chunk = scope.read(scope.in_waiting or 4096)
        if chunk:
            dados += chunk
        time.sleep(0.1)
        # Para quando não chegam mais bytes
        if dados and scope.in_waiting == 0:
            time.sleep(0.3)
            if scope.in_waiting == 0:
                break

    if not dados:
        raise RuntimeError("Nenhum dado recebido do osciloscópio.")

    # Converte bytes para array de floats
    texto = dados.decode('latin-1').strip()
    valores = []
    for linha in texto.split('\n'):
        linha = linha.strip()
        if not linha:
            continue
        try:
            valores.append(float(linha))
        except ValueError:
            continue

    if not valores:
        raise RuntimeError(f"Não foi possível parsear os dados: {texto[:100]}")

    V = np.array(valores)

    # Reconstrói eixo de tempo a partir da timebase
    # TDIV * 10 divisões = duração total; dt = duração / npontos
    tdiv_str = _cmd(scope, 'TDIV?', aguarda=0.3)
    try:
        tdiv = float(tdiv_str)
    except:
        tdiv = 50e-6  # fallback 50 µs/div

    duracao = tdiv * 10         # 10 divisões na tela
    dt = duracao / len(V)
    t = np.arange(len(V)) * dt - duracao / 2  # trigger no centro

    return t, V


# ══════════════════════════════════════════════════════════════════════════════
# FUNÇÕES DE CONTROLE DO BEAM DUMPER
# ══════════════════════════════════════════════════════════════════════════════

def abrir_beam_dumper(arduino):
    """Abre o beam dumper — laser passa para a célula."""
    arduino.write(b'O')
    time.sleep(0.3)  # aguarda o servo mover


def fechar_beam_dumper(arduino):
    """Fecha o beam dumper — laser bloqueado."""
    arduino.write(b'C')
    time.sleep(0.3)


# ══════════════════════════════════════════════════════════════════════════════
# FITTING BIEXPONENCIAL
# ══════════════════════════════════════════════════════════════════════════════

def ajuste_biexponencial(t, V):
    """
    Ajusta decaimento biexponencial ao TPV.
    Retorna dicionário com parâmetros ou None se falhar.

    Modelo: V(t) = A1*exp(-t/tau1) + A2*exp(-t/tau2) + V_inf
    """
    # Acha o pico
    idx_pico = np.argmax(V)
    if idx_pico >= len(V) - 10:
        return None

    t_dec = t[idx_pico:] - t[idx_pico]
    V_dec = V[idx_pico:]

    # Baseline: média dos últimos 10%
    baseline = np.mean(V_dec[int(len(V_dec) * 0.9):])
    V_corr = V_dec - baseline

    if V_corr[0] <= 0:
        return None

    # Subsampling para fit mais rápido
    step = max(1, len(t_dec) // 1000)
    t_fit = t_dec[::step]
    V_fit = V_corr[::step]

    def biexp(t, A1, tau1, A2, tau2):
        return A1 * np.exp(-t / tau1) + A2 * np.exp(-t / tau2)

    try:
        A0 = V_corr[0]
        popt, pcov = curve_fit(
            biexp, t_fit, V_fit,
            p0=[A0 * 0.6, 10e-6, A0 * 0.4, 100e-6],
            bounds=(0, [10, 1e-2, 10, 1e-2]),
            maxfev=10000
        )
        A1, tau1, A2, tau2 = popt
        perr = np.sqrt(np.diag(pcov))

        # Ordena: tau1 < tau2
        if tau1 > tau2:
            A1, tau1, A2, tau2 = A2, tau2, A1, tau1
            perr = perr[[2, 3, 0, 1]]

        return {
            'A1':    A1,    'tau1':   tau1,
            'A2':    A2,    'tau2':   tau2,
            'dA1':   perr[0], 'dtau1': perr[1],
            'dA2':   perr[2], 'dtau2': perr[3],
            'Vpico': V_corr[0],
            'baseline': baseline,
            't_pico': t[idx_pico],
        }
    except Exception as e:
        print(f"    Aviso: fit biexponencial falhou — {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# ROTINA PRINCIPAL DE MEDIDA
# ══════════════════════════════════════════════════════════════════════════════

def medir_tpv(scope, arduino, intensidade_pct, idx):
    print(f"\n  [Intensidade {intensidade_pct}%]")

    # Garante beam dumper fechado
    fechar_beam_dumper(arduino)

    # Estabilização iônica com luz de bias (sem laser)
    print(f"  Estabilizando por {TEMPO_ESTABILIZACAO} s...")
    time.sleep(TEMPO_ESTABILIZACAO)

    # Arma trigger ANTES de abrir o dump
    armar_trigger(scope)
    print("  Trigger armado.")

    # Abre beam dumper — laser começa a chegar na amostra
    # O próximo pulso do laser (a cada ~33 ms) vai triggar o osciloscópio
    print("  Abrindo beam dumper...")
    abrir_beam_dumper(arduino)

    # Aguarda trigger disparar
    print("  Aguardando disparo do laser...")
    ok = aguardar_aquisicao(scope)

    # Fecha beam dumper imediatamente
    fechar_beam_dumper(arduino)
    print("  Beam dumper fechado.")

    if not ok:
        print("  AVISO: timeout — nenhum trigger recebido.")
        return None

    print("  Aquisição completa. Baixando dados...")

    # Baixa waveform
    try:
        t, V = baixar_waveform(scope)
    except Exception as e:
        print(f"  ERRO ao baixar waveform: {e}")
        return None

    print(f"  {len(V)} pontos recebidos.")

    # Salva CSV bruto
    os.makedirs(PASTA_SAIDA, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    nome_csv = f"{AMOSTRA}_TPV_{intensidade_pct}pct_{timestamp}.csv"
    caminho_csv = os.path.join(PASTA_SAIDA, nome_csv)

    df = pd.DataFrame({'Time (s)': t, 'Voltage (V)': V})
    df.to_csv(caminho_csv, index=False)
    print(f"  Salvo: {nome_csv}")

    # Fitting biexponencial
    fit = ajuste_biexponencial(t, V)
    if fit:
        print(f"  Fit: τ₁ = {fit['tau1']*1e6:.1f} µs, "
              f"τ₂ = {fit['tau2']*1e6:.1f} µs, "
              f"ΔV = {fit['Vpico']*1e3:.2f} mV")
    else:
        print("  Fit não convergiu.")

    return {
        'intensidade_pct': intensidade_pct,
        'arquivo': nome_csv,
        't': t,
        'V': V,
        'fit': fit,
    }


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("  TPV — Medida Automatizada")
    print(f"  Amostra: {AMOSTRA}")
    print(f"  Intensidades: {INTENSIDADES}")
    print("=" * 60)

    scope   = None
    arduino = None

    try:
        # Conecta instrumentos
        scope   = conectar_osciloscopio()
        arduino = conectar_arduino()

        # Fecha beam dumper como medida de segurança inicial
        fechar_beam_dumper(arduino)
        print("Beam dumper fechado (segurança inicial).")

        # Configura osciloscópio
        configurar_osciloscopio(scope)

        # Cria pasta de saída
        os.makedirs(PASTA_SAIDA, exist_ok=True)

        # Loop de medidas
        resultados = []
        print(f"\nIniciando varredura de {len(INTENSIDADES)} intensidades...")
        print("ATENÇÃO: certifique-se que o laser está ligado e aquecido.\n")
        input("Pressione ENTER para começar...")

        for idx, intensidade in enumerate(INTENSIDADES):
            resultado = medir_tpv(scope, arduino, intensidade, idx)
            if resultado:
                resultados.append(resultado)

        # Salva tabela de resultados consolidada
        if resultados:
            linhas = []
            for r in resultados:
                linha = {'intensidade_pct': r['intensidade_pct'],
                         'arquivo': r['arquivo']}
                if r['fit']:
                    linha.update({
                        'tau1_us':  r['fit']['tau1'] * 1e6,
                        'tau2_us':  r['fit']['tau2'] * 1e6,
                        'A1_V':     r['fit']['A1'],
                        'A2_V':     r['fit']['A2'],
                        'Vpico_mV': r['fit']['Vpico'] * 1e3,
                    })
                linhas.append(linha)

            df_res = pd.DataFrame(linhas)
            nome_resumo = os.path.join(
                PASTA_SAIDA,
                f"{AMOSTRA}_TPV_resumo_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            )
            df_res.to_csv(nome_resumo, index=False)
            print(f"\nResumo salvo: {nome_resumo}")
            print(df_res.to_string(index=False))

        print("\nMedida concluída.")

    except KeyboardInterrupt:
        print("\n\nInterrompido pelo usuário.")

    except Exception as e:
        print(f"\nERRO CRÍTICO: {e}")
        import traceback
        traceback.print_exc()

    finally:
        # Segurança: fecha beam dumper sempre ao encerrar
        if arduino:
            fechar_beam_dumper(arduino)
            print("Beam dumper fechado (encerramento).")
            arduino.close()
        if scope:
            _cmd(scope, 'TRMD AUTO')  # volta para modo auto
            scope.close()
        print("Instrumentos desconectados.")


if __name__ == '__main__':
    main()
