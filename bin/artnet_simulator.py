#!/usr/bin/env python3
"""
AI-OS Art-Net / DMX Simulator — Fase 5
Porta UDP 6454. Recebe pacotes Art-Net ArtDmx e guarda estado em runtime/dmx_state.json.
"""
import socket, struct, json, threading, time, logging, sys, os, pathlib

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

AIOS_ROOT  = pathlib.Path(os.environ.get("AIOS_ROOT", os.path.expanduser("~/ai-os"))).resolve()
STATE_FILE = AIOS_ROOT / "runtime" / "dmx_state.json"
UDP_PORT   = 6454

ARTNET_HEADER = b"Art-Net\x00"
OP_DMX        = 0x5000

# universo (int) → lista de 512 valores [0-255]
_universes: dict[int, list[int]] = {0: [0] * 512}
_lock = threading.Lock()


def _save_state():
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            data = {str(u): vals[:] for u, vals in _universes.items()}
        STATE_FILE.write_text(
            json.dumps({"universes": data, "ts": time.time()}, indent=2)
        )
    except Exception as e:
        log.warning(f"save state error: {e}")


def _parse_artdmx(data: bytes):
    """Analisa pacote Art-Net ArtDmx; devolve (universe, dmx_list) ou None."""
    if len(data) < 18 or not data.startswith(ARTNET_HEADER):
        return None
    opcode = struct.unpack_from("<H", data, 8)[0]
    if opcode != OP_DMX:
        return None
    # offset 12: ProtVer(2), 14: Sequence(1), 15: Physical(1),
    # 14: universe(2 LE), 16: length(2 BE), 18: data
    universe = struct.unpack_from("<H", data, 14)[0]
    length   = struct.unpack_from(">H", data, 16)[0]
    dmx_data = list(data[18: 18 + length])
    return universe, dmx_data


def _server():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", UDP_PORT))
    log.info(f"Art-Net UDP server a escutar em 0.0.0.0:{UDP_PORT}")

    while True:
        try:
            data, addr = sock.recvfrom(576)
            result = _parse_artdmx(data)
            if result:
                universe, dmx_data = result
                with _lock:
                    _universes[universe] = dmx_data + [0] * max(0, 512 - len(dmx_data))
                _save_state()
                if len(dmx_data) >= 4:
                    log.info(
                        f"ArtDmx  uni={universe}  "
                        f"R={dmx_data[0]}  G={dmx_data[1]}  B={dmx_data[2]}  "
                        f"dim={dmx_data[3]}  from={addr[0]}"
                    )
        except Exception as e:
            log.warning(f"recv error: {e}")


# inicializa estado em disco
_save_state()

threading.Thread(target=_server, daemon=True).start()

log.info("Art-Net simulator pronto. Ctrl-C para parar.")
try:
    while True:
        time.sleep(60)
except KeyboardInterrupt:
    log.info("Stopped.")
