"""Fast packet classification.

Parses the IP/TCP/UDP headers straight from the raw buffer - per-packet work is a
handful of ``struct.unpack_from`` calls instead of building pydivert header objects,
which matters because this runs for every packet on the wire.

It answers the three questions the engine asks per packet:

1. Which direction and address family is it, and who is the *remote* party?
2. Which local port does it belong to (so the process can be identified)?
3. Is it control traffic that must never be shaped?
"""

from __future__ import annotations

import ipaddress
import struct
from dataclasses import dataclass, field

from .filters import DEFAULT_EXCLUDED_BLOCKS, IPV6_EXCLUDED_PREFIXES

PROTO_ICMP = 1
PROTO_TCP = 6
PROTO_UDP = 17
PROTO_ICMPV6 = 58

DNS_PORT = 53

# Tamaños y desplazamientos de las cabeceras que leemos (RFC 791 y RFC 8200).
VERSION_4 = 4
VERSION_6 = 6
IPV4_MIN_HEADER = 20
IPV6_HEADER = 40
TCP_MIN_HEADER = 20
UDP_HEADER = 8
UDP_LENGTH_FIELD = 4
IPV4_PROTO_FIELD = 9
IPV6_NEXT_HEADER_FIELD = 6

# Cabeceras de extensión de IPv6 (RFC 8200/4302) y cuántas saltamos como máximo.
EXT_HOP_BY_HOP = 0
EXT_ROUTING = 43
EXT_FRAGMENT = 44
EXT_AH = 51
EXT_DSTOPTS = 60
EXT_NO_NEXT = 59
EXT_FRAGMENT_LEN = 8
MAX_EXT_HEADERS = 8
EXT_HEADERS = frozenset({EXT_HOP_BY_HOP, EXT_ROUTING, EXT_FRAGMENT, EXT_AH, EXT_DSTOPTS})


def _build_excluded_v4() -> tuple[ipaddress.IPv4Network, ...]:
    nets: list[ipaddress.IPv4Network] = []
    for lo, hi in DEFAULT_EXCLUDED_BLOCKS:
        nets.extend(
            ipaddress.summarize_address_range(ipaddress.IPv4Address(lo), ipaddress.IPv4Address(hi))
        )
    return tuple(nets)


EXCLUDED_V4: tuple[ipaddress.IPv4Network, ...] = _build_excluded_v4()
EXCLUDED_V6: tuple[ipaddress.IPv6Network, ...] = tuple(
    ipaddress.ip_network(p) for p in IPV6_EXCLUDED_PREFIXES
)


#: Los mismos rangos locales que ``EXCLUDED_V4``, como (red, máscara) enteros.
EXCLUDED_V4_INT: tuple[tuple[int, int], ...] = tuple(
    (int(net.network_address), int(net.netmask)) for net in EXCLUDED_V4
)


def is_local_v4_int(addr: int) -> bool:
    """Versión rápida para IPv4 del filtro de LAN, sin construir objetos.

    Mismos rangos que ``EXCLUDED_V4`` (la fuente de verdad, y lo que usa el filtro del kernel),
    pero en enteros: en el camino por paquete esto es una decena de operaciones frente a construir
    dos objetos y parsear texto (~2,4 us, el 42 % del coste medido). ``tests/test_classify.py``
    comprueba que ambos caminos coinciden, para que no puedan divergir.
    """
    return addr == BROADCAST_V4 or any(addr >> shift == prefix for shift, prefix in _LOCAL_V4)


#: Rangos locales como (bits a desplazar, prefijo esperado). Mismos rangos que ``EXCLUDED_V4``,
#: que sigue siendo la fuente de verdad; ``test_classify`` comprueba que ambos coinciden.
_LOCAL_V4: tuple[tuple[int, int], ...] = (
    (24, 127),  # 127.0.0.0/8
    (24, 10),  # 10.0.0.0/8
    (20, 0xAC1),  # 172.16.0.0/12
    (16, 0xC0A8),  # 192.168.0.0/16
    (16, 0xA9FE),  # 169.254.0.0/16
    (28, 0xE),  # 224.0.0.0/4
)

#: 255.255.255.255 (broadcast limitado).
BROADCAST_V4 = 0xFFFFFFFF


def is_local_remote(addr: str) -> bool:
    """True if this *remote* address belongs to a local/never-throttled range.

    Mirrors the kernel filter exactly, so user space and the driver agree on what
    "local traffic" means. IPv6 needs this because the filter compiler cannot
    compare 128-bit addresses.
    """
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if ip.version == VERSION_4:
        return any(ip in net for net in EXCLUDED_V4)
    return any(ip in net for net in EXCLUDED_V6)


@dataclass(slots=True)
class PacketInfo:
    """What the engine needs to know about one packet."""

    outbound: bool
    version: int
    proto: int
    local_port: int  # -1 when unknown (non-first fragment)
    remote_port: int
    #: Dirección remota como entero (4 o 16 bytes en orden de red). Guardarla así evita
    #: construir objetos por paquete; el texto se calcula solo si alguien lo pide.
    remote_int: int
    size: int  # bytes on the wire (IP total length as captured)
    payload_len: int  # transport payload (0 for pure ACKs)
    tcp_flags: int
    #: Caché perezosa del texto de ``remote_int``.
    _remote_text: str | None = field(default=None, repr=False, compare=False)

    @property
    def remote_addr(self) -> str:
        """Dirección remota en texto (se calcula la primera vez que se pide)."""
        if self._remote_text is None:
            self._remote_text = _format_addr(self.version, self.remote_int)
        return self._remote_text

    @property
    def is_tcp(self) -> bool:
        return self.proto == PROTO_TCP

    @property
    def is_udp(self) -> bool:
        return self.proto == PROTO_UDP

    @property
    def is_icmp(self) -> bool:
        return self.proto in (PROTO_ICMP, PROTO_ICMPV6)

    @property
    def is_pure_ack(self) -> bool:
        """TCP packet with no payload: an acknowledgement, never worth shaping."""
        return self.proto == PROTO_TCP and self.payload_len == 0

    @property
    def is_control(self) -> bool:
        """Traffic that must bypass the shaper: pure ACKs, DNS, ICMP."""
        if self.is_icmp or self.is_pure_ack:
            return True
        return bool(self.proto == PROTO_UDP and DNS_PORT in (self.local_port, self.remote_port))

    @property
    def is_local_remote(self) -> bool:
        """True si la dirección remota es local (LAN, loopback, multicast...)."""
        if self.version == VERSION_4:
            return is_local_v4_int(self.remote_int)
        return is_local_remote(self.remote_addr)  # IPv6: raro, aquí no compensa

    def describe(self) -> str:
        d = "out" if self.outbound else "in"
        return f"{d} ipv{self.version} proto={self.proto} :{self.local_port} -> {self.remote_addr}:{self.remote_port}"


def _addr_v4(raw) -> tuple[int, int]:
    """Direcciones IPv4 como enteros, sin construir objetos.

    Antes se creaban dos ``IPv4Address`` y se pasaban a texto en *cada* paquete, y el filtro de
    LAN volvía a parsear ese texto: ~2,4 us por paquete, el 42 % del camino (medido con
    ``tools/packet_bench.py``). Para decidir basta el entero.

    ``struct.unpack_from`` funciona con ``bytes`` y con ``memoryview``. Rebanar un memoryview
    (``raw[12:16]``) devuelve otro memoryview, que ``IPv4Address`` no sabe parsear: ese fue el
    fallo que una vez dejó al usuario sin internet (los paquetes llegan como memoryview).
    """
    src, dst = struct.unpack_from("!II", raw, 12)
    return src, dst


def _addr_v6(raw) -> tuple[int, int]:
    src, dst = struct.unpack_from("!16s16s", raw, 8)
    return int.from_bytes(src, "big"), int.from_bytes(dst, "big")


def _format_addr(version: int, value: int) -> str:
    if version == VERSION_4:
        return str(ipaddress.IPv4Address(value))
    return str(ipaddress.IPv6Address(value))


def inspect(raw: bytes | memoryview, outbound: bool) -> PacketInfo | None:
    """Parse a captured frame. Returns ``None`` if it is not a packet we handle.

    Accepts ``bytes``, ``bytearray`` and ``memoryview`` (what pydivert actually gives us).
    """
    if len(raw) < IPV4_MIN_HEADER:
        return None
    version = raw[0] >> 4

    if version == VERSION_4:
        header_len = (raw[0] & 0x0F) * 4
        if header_len < IPV4_MIN_HEADER or len(raw) < header_len:
            return None
        total_len = struct.unpack_from("!H", raw, 2)[0]
        proto = raw[IPV4_PROTO_FIELD]
        frag_offset = struct.unpack_from("!H", raw, 6)[0] & 0x1FFF
        src, dst = _addr_v4(raw)
        ip_header_end = header_len
        if frag_offset != 0:
            # A fragment past the first one has no transport header to look at.
            proto_known = proto
            return PacketInfo(
                outbound,
                VERSION_4,
                proto_known,
                -1,
                -1,
                dst if outbound else src,
                max(total_len, len(raw)),
                0,
                0,
            )
    elif version == VERSION_6:
        if len(raw) < IPV6_HEADER:
            return None
        payload_len = struct.unpack_from("!H", raw, 4)[0]
        total_len = IPV6_HEADER + payload_len
        proto = raw[IPV6_NEXT_HEADER_FIELD]
        src, dst = _addr_v6(raw)
        ip_header_end = IPV6_HEADER
        # Walk extension headers to find the real transport protocol.
        guard = 0
        while proto in EXT_HEADERS and guard < MAX_EXT_HEADERS:
            if len(raw) < ip_header_end + 2:
                return None
            nxt = raw[ip_header_end]
            if proto == EXT_FRAGMENT:
                step = EXT_FRAGMENT_LEN
            elif proto == EXT_AH:
                step = (raw[ip_header_end + 1] + 2) * 4
            else:
                step = (raw[ip_header_end + 1] + 1) * 8
            if step <= 0:
                return None
            ip_header_end += step
            proto = nxt
            guard += 1
        if proto in (EXT_FRAGMENT, EXT_NO_NEXT):
            return PacketInfo(
                outbound,
                VERSION_6,
                proto,
                -1,
                -1,
                dst if outbound else src,
                max(total_len, len(raw)),
                0,
                0,
            )
    else:
        return None

    size = max(int(total_len), len(raw))
    local_port = remote_port = -1
    payload_len = 0
    tcp_flags = 0

    if proto == PROTO_TCP and len(raw) >= ip_header_end + TCP_MIN_HEADER:
        sport, dport = struct.unpack_from("!HH", raw, ip_header_end)
        data_offset = (raw[ip_header_end + 12] >> 4) * 4
        tcp_flags = raw[ip_header_end + 13]
        transport_len = max(size - ip_header_end - data_offset, 0)
        payload_len = transport_len
        local_port, remote_port = (sport, dport) if outbound else (dport, sport)
    elif proto == PROTO_UDP and len(raw) >= ip_header_end + UDP_HEADER:
        sport, dport = struct.unpack_from("!HH", raw, ip_header_end)
        udp_len = struct.unpack_from("!H", raw, ip_header_end + UDP_LENGTH_FIELD)[0]
        payload_len = max(udp_len - UDP_HEADER, 0)
        local_port, remote_port = (sport, dport) if outbound else (dport, sport)
    elif proto in (PROTO_ICMP, PROTO_ICMPV6):
        local_port = remote_port = -1

    return PacketInfo(
        outbound=outbound,
        version=version,
        proto=proto,
        local_port=local_port,
        remote_port=remote_port,
        remote_int=dst if outbound else src,
        size=size,
        payload_len=payload_len,
        tcp_flags=tcp_flags,
    )


def should_pass_through(info: PacketInfo, *, exempt_control: bool, limit_ipv6: bool) -> bool:
    """True when the packet must be reinjected immediately, without shaping."""
    if info.local_port < 0:
        # Fragment we cannot attribute to a process: never hold it back.
        return True
    if info.version == VERSION_6 and not limit_ipv6:
        return True
    if info.is_local_remote:
        return True
    return bool(exempt_control and info.is_control)


def inspect_safe(raw: bytes | memoryview, outbound: bool) -> PacketInfo | None:
    """``inspect`` that never raises.

    Any parsing surprise returns ``None`` instead of killing the capture thread. A thread
    that dies while WinDivert holds packets means no internet at all, so the packet path
    must be incapable of propagating exceptions.
    """
    try:
        return inspect(raw, outbound)
    except Exception:
        return None


#: Synthetic packets used by :func:`self_test` and by the tests (TCP from 51000 to 443).
SELF_TEST_IPV4 = bytes(
    [
        0x45,
        0x00,
        0x00,
        0x28,
        0x00,
        0x00,
        0x00,
        0x00,
        0x40,
        0x06,
        0x00,
        0x00,
        192,
        168,
        1,
        10,
        8,
        8,
        8,
        8,
        0xC7,
        0x38,
        0x01,
        0xBB,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0x50,
        0x02,
        0xFF,
        0xFF,
        0,
        0,
        0,
        0,
    ]
)
SELF_TEST_IPV6 = bytes(
    [0x60, 0x00, 0x00, 0x00, 0x00, 0x14, 0x06, 0x40, 0xFE, 0x80]
    + [0] * 13
    + [0x01]
    + [
        0x20,
        0x01,
        0x48,
        0x60,
        0x48,
        0x60,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0x88,
        0x88,
        0xC7,
        0x38,
        0x01,
        0xBB,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0x50,
        0x02,
        0xFF,
        0xFF,
        0,
        0,
        0,
        0,
    ]
)

#: (raw packet, version, local IP as a string, remote address, remote endpoint)
_SELF_TEST_CASES = (
    (SELF_TEST_IPV4, 4, "192.168.1.10", "8.8.8.8"),
    (SELF_TEST_IPV6, 6, "fe80::1", "2001:4860:4860::8888"),
)


def self_test() -> tuple[bool, str]:
    """Verify the parser against the input type the driver hands us.

    Runs before any handle is opened: a parser that cannot cope with ``memoryview`` must
    abort the start instead of blackholing the connection (which is exactly what happened
    once, and is why this exists).
    """
    for raw, version, local_ip, remote_ip in _SELF_TEST_CASES:
        for view in (memoryview(raw), bytes(raw), bytearray(raw)):
            out = inspect_safe(view, outbound=True)
            if out is None:
                return False, f"no se pudo analizar un paquete ipv{version} saliente"
            if (out.version, out.local_port, out.remote_port) != (version, 51000, 443):
                return False, (
                    f"puertos incorrectos al analizar ipv{version}: "
                    f"{out.local_port} -> {out.remote_port}"
                )
            if out.remote_addr != remote_ip:
                return False, f"dirección remota incorrecta en ipv{version}: {out.remote_addr}"

            incoming = inspect_safe(view, outbound=False)
            if incoming is None:
                return False, f"no se pudo analizar un paquete ipv{version} entrante"
            if (incoming.local_port, incoming.remote_port) != (443, 51000):
                return False, f"puertos incorrectos en sentido entrante (ipv{version})"
            if incoming.remote_addr != local_ip:
                return False, f"dirección remota incorrecta en entrada (ipv{version})"

    for broken in (b"", b"\x45", b"\x45\x00\x00", b"\x00" * 8):
        if inspect_safe(memoryview(broken), outbound=True) is not None:
            return False, f"un paquete inválido debería descartarse ({len(broken)} bytes)"
    return True, "análisis de paquetes verificado"
