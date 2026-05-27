# TPV/TPC Data Collection — Teledyne LeCroy WaveJet 334T

Software desktop para coleta de waveforms de TPV e TPC em células solares de perovskita.

## Instalação

```bash
pip install -r requirements.txt
```

## Execução

```bash
cd tpv_app
python main.py
```

## Configuração do hardware (feita no painel físico, não pelo software)

| Parâmetro | TPV | TPC |
|-----------|-----|-----|
| Canal da célula | CH3 | CH2 |
| Terminação | 1 MΩ | 50 Ω |
| CH1 | Trigger Q-switch do laser | idem |

Configure timebase, V/div e trigger diretamente no osciloscópio antes de coletar.

## Detecção automática de portas

- **Osciloscópio**: envia `*IDN?` em cada porta COM e verifica "LECROY" na resposta.
- **Arduino Mega**: detectado por VID `0x2341` (oficial) ou `0x1A86` (CH340), ou pela descrição da porta contendo "Arduino", "CH340" ou "ATMEGA".

## Formato do CSV gerado

```
# Amostra: <nome_base>
# Tipo: TPV|TPC
# Data: <ISO 8601>
# Timebase (s/div): <valor>
# N_pontos: <N>
Time (s),CH1 (V),CH2 (V),CH3 (V),Normalized
...
```

A coluna `Normalized` usa o canal ativo (CH3 para TPV, CH2 para TPC):  
`(V − baseline) / (V_pico − baseline)`, onde baseline = média dos primeiros 5% dos pontos.

## Estrutura

```
tpv_app/
├── main.py        # entry point
├── gui.py         # MainWindow (PyQt6)
├── hardware.py    # OsciloscopioController, ArduinoController
├── core.py        # processamento e salvamento CSV
├── styles.qss     # tema claro
└── requirements.txt
```
