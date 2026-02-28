#!/usr/bin/env python3
"""
AI-OS DMX Tools — Fase 5
Envia pacotes Art-Net ArtDmx ao simulador e lê/escreve runtime/dmx_state.json.
Uso directo: python3 tools_dmx.py <tool> [json_params]
"""
import sys, json, os, socket, struct, pathlib, time

AIOS_ROOT   = pathlib.Path(os.environ.get("AIOS_ROOT", os.path.expanduser("~/ai-os"))).resolve()
STATE_FILE  = AIOS_ROOT / "runtime" / "dmx_state.json"
ARTNET_HOST = os.environ.get("ARTNET_HOST", "127.0.0.1")
ARTNET_PORT = int(os.environ.get("ARTNET_PORT", "6454"))

ARTNET_HEADER = b"Art-Net\x00"

SCENES: dict[str, dict[int, list[int]]] = {
    "blackout": {0: [0] * 512},
    "red":      {0: [255,   0,   0, 255] + [0] * 508},
    "green":    {0: [  0, 255,   0, 255] + [0] * 508},
    "blue":     {0: [  0,   0, 255, 255] + [0] * 508},
    "white":    {0: [255, 255, 255, 255] + [0] * 508},
    "warm":     {0: [255, 160,  30, 200] + [0] * 508},
    "cyan":     {0: [  0, 255, 255, 255] + [0] * 508},
    "magenta":  {0: [255,   0, 255, 255] + [0] * 508},
}


def _make_artdmx(universe: int, dmx_data: list, seq: int = 0) -> bytes:
    length = min(len(dmx_data), 512)
    if length % 2 != 0:
        length += 1
    pkt = bytearray()
    pkt += ARTNET_HEADER
    pkt += struct.pack("<H", 0x5000)   # OpDmx
    pkt += struct.pack(">H", 14)       # ProtVer
    pkt += bytes([seq % 256, 0])       # Sequence, Physical
    pkt += struct.pack("<H", universe)
    pkt += struct.pack(">H", length)
    raw = dmx_data[:length]
    pkt += bytes(raw)
    if length > len(raw):
        pkt += bytes(length - len(raw))
    return bytes(pkt)


def _send_artdmx(universe: int, dmx_data: list):
    pkt = _make_artdmx(universe, dmx_data)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(pkt, (ARTNET_HOST, ARTNET_PORT))
    except Exception:
        pass  # simulador pode não estar a correr; estado já foi salvo


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"universes": {"0": [0] * 512}, "ts": 0}


def _write_state(universes: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps({"universes": {str(k): v for k, v in universes.items()}, "ts": time.time()}, indent=2)
    )


def tool_dmx_set(params):
    """Define canal(is) DMX.
    params: {universe?, channel, value}
       ou   {universe?, channels: {"1": 255, "2": 0, ...}}
    """
    universe = int(params.get("universe", 0))
    state    = _load_state()
    dmx      = list(state.get("universes", {}).get(str(universe), [0] * 512))
    while len(dmx) < 512:
        dmx.append(0)

    changed = 0

    # canal único
    ch  = params.get("channel")
    val = params.get("value")
    if ch is not None and val is not None:
        idx = int(ch) - 1  # 1-indexed
        if 0 <= idx < 512:
            dmx[idx] = max(0, min(255, int(val)))
            changed += 1

    # multi canais
    for c, v in params.get("channels", {}).items():
        idx = int(c) - 1
        if 0 <= idx < 512:
            dmx[idx] = max(0, min(255, int(v)))
            changed += 1

    # persiste e envia Art-Net
    all_unis = {int(k): v for k, v in state.get("universes", {}).items()}
    all_unis[universe] = dmx
    _write_state(all_unis)
    _send_artdmx(universe, dmx)

    return {"ok": True, "universe": universe, "channels_set": changed}


def tool_dmx_status(params):
    """Estado actual dos universos DMX."""
    state    = _load_state()
    universes = state.get("universes", {})
    result   = {}
    for uni_str, dmx in universes.items():
        result[uni_str] = {
            "R":      dmx[0] if len(dmx) > 0 else 0,
            "G":      dmx[1] if len(dmx) > 1 else 0,
            "B":      dmx[2] if len(dmx) > 2 else 0,
            "dimmer": dmx[3] if len(dmx) > 3 else 0,
            "ch1_8":  dmx[:8] if len(dmx) >= 8 else dmx,
            "active": any(v > 0 for v in dmx),
        }
    return {"ok": True, "universes": result, "ts": state.get("ts")}


def tool_dmx_scene(params):
    """Aplica cena pré-definida.
    params: {scene: 'red'|'blue'|'green'|'blackout'|'white'|'warm'|'cyan'|'magenta'}
    """
    name = params.get("scene", "blackout").lower()
    if name not in SCENES:
        return {
            "ok": False,
            "error": f"cena desconhecida: {name}",
            "available": list(SCENES.keys()),
        }
    state    = _load_state()
    all_unis = {int(k): v for k, v in state.get("universes", {}).items()}
    for uni, dmx in SCENES[name].items():
        all_unis[uni] = dmx
        _send_artdmx(uni, dmx)
    _write_state(all_unis)
    return {"ok": True, "scene": name, "universes": list(SCENES[name].keys())}


TOOLS = {
    "dmx_set":    tool_dmx_set,
    "dmx_status": tool_dmx_status,
    "dmx_scene":  tool_dmx_scene,
}

if __name__ == "__main__":
    fn     = sys.argv[1] if len(sys.argv) >= 2 else "dmx_status"
    params = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    fn_obj = TOOLS.get(fn)
    print(json.dumps(
        fn_obj(params) if fn_obj else {"ok": False, "error": f"tool desconhecida: {fn}"}
    ))
