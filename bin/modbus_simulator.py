#!/usr/bin/env python3
"""
AI-OS Modbus TCP Simulator — Fase 4
Porta 5020. Holding registers (função 3):
  addr 0 (40001): Temperatura  (20–80 °C × 10, ex: 350 = 35.0°C)
  addr 1 (40002): Pressão      (1–10 bar × 10, ex: 50 = 5.0 bar)
  addr 2 (40003): RPM motor    (0–3000)
  addr 3 (40004): Estado       (0=off, 1=on, 2=alarm)
"""
import time, random, threading, logging, sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

try:
    from pymodbus.server import StartTcpServer
    from pymodbus.datastore import (
        ModbusSequentialDataBlock,
        ModbusDeviceContext,
        ModbusServerContext,
    )
except ImportError as exc:
    log.error(f"pymodbus não instalado ({exc}). Corre: pip install pymodbus --break-system-packages")
    sys.exit(1)

HOST = "0.0.0.0"
PORT = 5020

# Valores actuais [temp*10, pressão*10, rpm, estado]
_vals = [350, 50, 1500, 1]  # 35.0°C, 5.0bar, 1500rpm, on

block   = ModbusSequentialDataBlock(0, _vals + [0] * 60)
device  = ModbusDeviceContext(hr=block)
context = ModbusServerContext(devices=device, single=True)


def _updater():
    """Actualiza registos a cada 2 s com variação aleatória."""
    while True:
        time.sleep(2)
        try:
            temp  = _vals[0] + random.randint(-15, 15)
            temp  = max(200, min(800, temp))

            press = _vals[1] + random.randint(-5, 5)
            press = max(10, min(100, press))

            rpm   = _vals[2] + random.randint(-80, 80)
            rpm   = max(0, min(3000, rpm))

            state = _vals[3]
            if random.random() < 0.02:   # 2% chance alarme
                state = 2
            elif state == 2 and random.random() < 0.3:
                state = 1

            _vals[:] = [temp, press, rpm, state]
            context[0].setValues(3, 0, _vals)
            log.info(
                f"sensors  temp={temp/10:.1f}°C  press={press/10:.1f}bar"
                f"  rpm={rpm}  state={state}"
            )
        except Exception as e:
            log.warning(f"updater error: {e}")


threading.Thread(target=_updater, daemon=True).start()

log.info(f"Modbus TCP simulator a correr em {HOST}:{PORT}")
StartTcpServer(context=context, address=(HOST, PORT))
