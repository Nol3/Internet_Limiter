"""Cuánto cuesta cada paquete en el motor, y en qué se va el tiempo.

El limitador añade latencia porque el hilo de captura no da abasto: con una descarga a tope
pasan ~30.000 paquetes/s y si cada uno cuesta más de ~33 us el hilo se queda por detrás de lo que
llega, la cola del driver se llena (es FIFO) y tus paquetes de juego esperan detrás.

Este banco mide ese coste con un descriptor falso (sin driver, sin administrador, en segundos) y
perfila el reparto, para optimizar lo que pesa de verdad.

    python tools\\packet_bench.py            # coste por paquete
    python tools\\packet_bench.py --profile  # además, reparto por función
"""

from __future__ import annotations

import argparse
import cProfile
import io
import ipaddress
import pstats
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from limitador.console import ensure_utf8_console
from limitador.engine import Engine
from limitador.rules import AppRule, Config, Settings

MB = 1024 * 1024
#: Paquetes por tanda. 30.000 es lo que pasa en un segundo de descarga a tope.
BATCH = 30000


class FakePacket:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw


class CountingHandle:
    """Descriptor mínimo: solo necesita aceptar los envíos."""

    def __init__(self) -> None:
        self.sent = 0
        self.bytes = 0

    def send(self, packet, recalculate_checksum: bool = True) -> None:
        self.sent += 1
        self.bytes += len(packet.raw)


def _ipv4(src: str, dst: str, proto: int, payload_len: int, sport: int, dport: int) -> bytes:
    total = 20 + payload_len
    ip = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        total,
        0,
        0,
        64,
        proto,
        0,
        ipaddress.IPv4Address(src).packed,
        ipaddress.IPv4Address(dst).packed,
    )
    if proto == 6:
        transport = struct.pack("!HHIIBBHHH", sport, dport, 0, 0, (5 << 4), 0x10, 8192, 0, 0)
        body = b"\x00" * max(payload_len - 20, 0)
    elif proto == 17:
        transport = struct.pack("!HHHH", sport, dport, 8 + max(payload_len - 8, 0), 0)
        body = b"\x00" * max(payload_len - 8, 0)
    else:  # ICMP
        transport = struct.pack("!BBHHH", 8, 0, 0, 1, 1)
        body = b"\x00" * max(payload_len - 8, 0)
    return ip + transport + body


def build_mix(count: int) -> list[FakePacket]:
    """Reparto realista de una descarga: mucho dato a tamaño completo, ACKs, algo de control."""
    packets: list[FakePacket] = []
    for i in range(count):
        pick = i % 20
        if pick < 15:  # 75 %: dato a tamaño completo (bajada)
            packets.append(FakePacket(_ipv4("8.8.8.8", "192.168.1.10", 6, 1440, 443, 51000)))
        elif pick < 18:  # 15 %: ACKs de subida (sin carga)
            packets.append(FakePacket(_ipv4("192.168.1.10", "8.8.8.8", 6, 20, 51000, 443)))
        elif pick < 19:  # 5 %: DNS
            packets.append(FakePacket(_ipv4("192.168.1.10", "8.8.8.8", 17, 40, 53000, 53)))
        else:  # 5 %: ICMP
            packets.append(FakePacket(_ipv4("192.168.1.10", "8.8.8.8", 1, 60, 0, 0)))
    return packets


def run_once(engine: Engine, handle, packets: list[FakePacket]) -> float:
    started = time.perf_counter()
    process = engine._process_packet
    for packet in packets:
        process("out", True, handle, packet)
    return time.perf_counter() - started


def report(label: str, seconds: float, count: int) -> float:
    per_packet = seconds / count
    rate = count / seconds
    print(
        f"   {label:<34} {per_packet * 1e6:6.1f} us/paquete  {rate / 1000:7.1f}k paquetes/s"
        f"  ({1 / per_packet:.0f}% de un nucleo)"
    )
    return per_packet


def main() -> int:
    ensure_utf8_console()
    parser = argparse.ArgumentParser(description="Coste por paquete del motor.")
    parser.add_argument("--count", type=int, default=BATCH, help="paquetes por tanda")
    parser.add_argument("--profile", action="store_true", help="perfilar el reparto del tiempo")
    args = parser.parse_args()

    packets = build_mix(args.count)
    handle = CountingHandle()

    # 1) Motor sin límites: el camino rápido (parsear, atribuir, reinyectar).
    plain = Engine(Config(settings=Settings()))
    run_once(plain, handle, packets[:2000])  # calentar
    plain_seconds = min(run_once(plain, handle, packets) for _ in range(3))

    # 2) Con límite global: además pasa por la cola y el planificador.
    limited = Engine(Config(settings=Settings(global_down_bps=8 * MB, global_up_bps=8 * MB)))
    limited_seconds = min(run_once(limited, handle, packets) for _ in range(3))

    # 3) Con regla de prioridad: el juego se libra del planificador.
    priority = Engine(Config(settings=Settings(global_down_bps=8 * MB, global_up_bps=8 * MB)))
    priority.config.set_rule(AppRule(exe=r"C:\juego.exe", exempt=True))
    priority_seconds = min(run_once(priority, handle, packets) for _ in range(3))

    print(f"\nCoste por paquete ({args.count} paquetes por tanda, mejor de 3):\n")
    plain_us = report("sin límites (camino rápido)", plain_seconds, args.count)
    report("con límite global (cola)", limited_seconds, args.count)
    report("con regla de prioridad", priority_seconds, args.count)
    budget = 1e6 / 30000
    print(f"\n  Presupuesto: {budget:.1f} us/paquete para sostener 30.000 paquetes/s en un núcleo.")
    print(
        f"  Con {plain_us:.1f} us/paquete el hilo aguanta {1e6 / plain_us / 1000:.0f}k paquetes/s."
        + (
            "  -> va sobrado."
            if plain_us < budget
            else "  -> NO da abasto: la cola del driver crece."
        )
    )

    if args.profile:
        print("\nReparto del tiempo (camino sin límites):")
        profiler = cProfile.Profile()
        profiler.enable()
        run_once(plain, handle, packets)
        profiler.disable()
        out = io.StringIO()
        stats = pstats.Stats(profiler, stream=out).sort_stats("tottime")
        stats.print_stats(12)
        for line in out.getvalue().splitlines():
            if line.strip() and ("{" not in line or "limitador" in line or "pydivert" in line):
                print("   " + line.rstrip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
