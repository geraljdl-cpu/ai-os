#!/usr/bin/env python3
"""
AI-OS Factory Tools — Fase 4
Lê sensores do simulador Modbus TCP (porta 5020).
Uso directo: python3 tools_factory.py <tool> [json_params]
"""
import sys, json, os

MODBUS_HOST = os.environ.get("MODBUS_HOST", "127.0.0.1")
MODBUS_PORT = int(os.environ.get("MODBUS_PORT", "5020"))

# addr → (nome, unidade, divisor)
_SENSORS = {
    0: ("temperatura", "°C",  10.0),
    1: ("pressao",     "bar", 10.0),
    2: ("rpm",         "rpm",  1.0),
    3: ("estado",      "",     1.0),
}
_STATE = {0: "off", 1: "on", 2: "alarm"}


def _read_regs(count=4):
    try:
        from pymodbus.client import ModbusTcpClient
    except ImportError:
        return {"ok": False, "error": "pymodbus não instalado"}
    try:
        c = ModbusTcpClient(MODBUS_HOST, port=MODBUS_PORT)
        if not c.connect():
            return {"ok": False, "error": f"sem ligação a {MODBUS_HOST}:{MODBUS_PORT}"}
        r = c.read_holding_registers(0, count=count)
        c.close()
        if r.isError():
            return {"ok": False, "error": str(r)}
        return {"ok": True, "registers": r.registers}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def tool_factory_read(params):
    """Lê todos os sensores e devolve valores físicos."""
    r = _read_regs(4)
    if not r["ok"]:
        return r
    regs = r["registers"]
    sensors = {}
    for addr, (name, unit, div) in _SENSORS.items():
        raw = regs[addr] if addr < len(regs) else 0
        val = raw / div
        sensors[name] = {"value": val, "unit": unit, "raw": raw}
    sensors["estado"]["label"] = _STATE.get(int(regs[3]), "unknown")
    return {"ok": True, "sensors": sensors}


def tool_factory_status(params):
    """Estado resumido da fábrica."""
    r = tool_factory_read(params)
    if not r["ok"]:
        return r
    s = r["sensors"]
    state_val = int(s.get("estado", {}).get("raw", 0))
    return {
        "ok":          True,
        "status":      _STATE.get(state_val, "unknown"),
        "temperatura": f"{s.get('temperatura', {}).get('value', 0):.1f}°C",
        "pressao":     f"{s.get('pressao', {}).get('value', 0):.1f} bar",
        "rpm":         int(s.get("rpm", {}).get("value", 0)),
    }


TOOLS = {
    "factory_read":   tool_factory_read,
    "factory_status": tool_factory_status,
}

if __name__ == "__main__":
    fn     = sys.argv[1] if len(sys.argv) >= 2 else "factory_status"
    params = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    fn_obj = TOOLS.get(fn)
    print(json.dumps(fn_obj(params) if fn_obj else {"ok": False, "error": f"tool desconhecida: {fn}"}))
