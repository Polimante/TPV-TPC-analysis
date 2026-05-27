import serial
import time

scope = serial.Serial('COM4', 9600, timeout=3)
time.sleep(0.5)

def cmd(scope, comando, aguarda=0.8):
    scope.reset_input_buffer()
    scope.write((comando + '\n').encode())
    time.sleep(aguarda)
    resp = scope.read(scope.in_waiting or 1)
    return resp.decode('latin-1').strip() if resp else '(sem resposta)'

print('=== Lendo configuração atual do trigger ===\n')

# Lê todas as configurações de trigger disponíveis
queries = [
    'TRIG_SELECT?',
    'TRMD?',
    'TRIG_SLOPE?',
    'TRIG_LEVEL?',
    'C1:TRIG_LEVEL?',
]

for q in queries:
    print(f'{q:25s} → {cmd(scope, q)}')

print('\n=== Testando configuração do trigger para CH1 ===\n')

# Configura trigger explicitamente para CH1
configs = [
    'TRIG_SELECT EDGE,SR,C1',
    'C1:TRIG_SLOPE POS',
    'C1:TRIG_LEVEL 1.5',
]

for c in configs:
    r = cmd(scope, c)
    print(f'{c:35s} → {r}')

# Confirma
print('\nConfirmando:')
print('TRIG_SELECT?  →', cmd(scope, 'TRIG_SELECT?'))
print('TRIG_SLOPE?   →', cmd(scope, 'TRIG_SLOPE?'))

# Arma e monitora
print('\nArmando trigger... dispare o laser manualmente.\n')
cmd(scope, 'TRMD SINGLE')

t0 = time.time()
while time.time() - t0 < 15:
    trmd = cmd(scope, 'TRMD?', aguarda=0.3)
    print(f't={time.time()-t0:.1f}s | TRMD={trmd}')
    if trmd and 'STOP' in trmd.upper():
        print('*** TRIGGER DISPAROU ***')
        break
    time.sleep(0.5)

scope.close()