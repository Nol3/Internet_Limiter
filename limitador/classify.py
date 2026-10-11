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
from dataclasses import dataclass

from .filters import DEFAULT_EXCLUDED_BLOCKS, IPV6_EXCLUDED_PREFIXES

PROTO_ICMP = 1
PROTO_TCP = 6
PROTO_UDP = 17
PROTO_ICMPV6 = 58

#: IPv6 extension headers we know how to skip (+ their size rules).
_EXT_HEADERS = {0, 43, 44, 51, 60, 59}

DNS_PORT = 53


def _build_excluded_v4() -> tuple[ipaddress.IPv4Network, ...]:
    nets: list[ipaddress.IPv4Network] = []
    for lo, hi in DEFAULT_EXCLUDED_BLOCKS:
        nets.extend(
            ipaddress.summarize_address_range(
                ipaddress.IPv4Address(lo), ipaddress.IPv4Address(hi)
            )
        )
    return tuple(nets)


EXCLUDED_V4: tuple[ipaddress.IPv4Network, ...] = _build_excluded_v4()
EXCLUDED_V6: tuple[ipaddress.IPv6Network, ...] = tuple(
    ipaddress.ip_network(p) for p in IPV6_EXCLUDED_PREFIXES
)


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
    if ip.version == 4:
        return any(ip in net for net in EXCLUDED_V4)
    return any(ip in net for net in EXCLUDED_V6)


@dataclass(slots=True)
class PacketInfo:
    """What the engine needs to know about one packet."""

    outbound: bool
    version: int
    proto: int
    local_port: int      # -1 when unknown (non-first fragment)
    remote_port: int
    remote_addr: str
    size: int            # bytes on the wire (IP total length as captured)
    payload_len: int     # transport payload (0 for pure ACKs)
    tcp_flags: int

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
        if self.proto == PROTO_UDP and DNS_PORT in (self.local_port, self.remote_port):
            return True
        return False

    @property
    def is_local_remote(self) -> bool:
        return is_local_remote(self.remote_addr)

    def describe(self) -> str:
        d = "out" if self.outbound else "in"
        return f"{d} ipv{self.version} proto={self.proto} :{self.local_port} -> {self.remote_addr}:{self.remote_port}"


def _addr_v4(raw) -> tuple[str, str]:
    # struct.unpack_from works on both bytes and memoryview. Slicing a memoryview with
    # ``raw[12:16]`` returns another memoryview, which IPv4Address cannot parse - that is
    # exactly the bug that blackholed the traffic once (packets arrive as memoryview).
    src, dst = struct.unpack_from("!4s4s", raw, 12)
    return str(ipaddress.IPv4Address(src)), str(ipaddress.IPv4Address(dst))


def _addr_v6(raw) -> tuple[str, str]:
    src, dst = struct.unpack_from("!16s16s", raw, 8)
    return str(ipaddress.IPv6Address(src)), str(ipaddress.IPv6Address(dst))


def inspect(raw: bytes | memoryview, outbound: bool) -> PacketInfo | None:
    """Parse a captured frame. Returns ``None`` if it is not a packet we handle.

    Accepts ``bytes``, ``bytearray`` and ``memoryview`` (what pydivert actually gives us).
    """
    if len(raw) < 20:
        return None
    version = raw[0] >> 4

    if version == 4:
        header_len = (raw[0] & 0x0F) * 4
        if header_len < 20 or len(raw) < header_len:
            return None
        total_len = struct.unpack_from("!H", raw, 2)[0]
        proto = raw[9]
        frag_offset = struct.unpack_from("!H", raw, 6)[0] & 0x1FFF
        src, dst = _addr_v4(raw)
        ip_header_end = header_len
        if frag_offset != 0:
            # A fragment past the first one has no transport header to look at.
            proto_known = proto
            return PacketInfo(outbound, 4, proto_known, -1, -1,
                              dst if outbound else src, max(total_len, len(raw)), 0, 0)
    elif version == 6:
        if len(raw) < 40:
            return None
        payload_len = struct.unpack_from("!H", raw, 4)[0]
        total_len = 40 + payload_len
        proto = raw[6]
        src, dst = _addr_v6(raw)
        ip_header_end = 40
        # Walk extension headers to find the real transport protocol.
        guard = 0
        while proto in (0, 43, 44, 51, 60) and guard < 8:
            if len(raw) < ip_header_end + 2:
                return None
            nxt = raw[ip_header_end]
            if proto == 44:
                step = 8
            elif proto == 51:
                step = (raw[ip_header_end + 1] + 2) * 4
            else:
                step = (raw[ip_header_end + 1] + 1) * 8
            if step <= 0:
                return None
            ip_header_end += step
            proto = nxt
            guard += 1
        if proto in (44, 59):
            return PacketInfo(outbound, 6, proto, -1, -1,
                              dst if outbound else src, max(total_len, len(raw)), 0, 0)
    else:
        return None

    size = max(int(total_len), len(raw))
    local_port = remote_port = -1
    payload_len = 0
    tcp_flags = 0

    if proto == PROTO_TCP and len(raw) >= ip_header_end + 20:
        sport, dport = struct.unpack_from("!HH", raw, ip_header_end)
        data_offset = (raw[ip_header_end + 12] >> 4) * 4
        tcp_flags = raw[ip_header_end + 13]
        transport_len = max(size - ip_header_end - data_offset, 0)
        payload_len = transport_len
        local_port, remote_port = (sport, dport) if outbound else (dport, sport)
    elif proto == PROTO_UDP and len(raw) >= ip_header_end + 8:
        sport, dport = struct.unpack_from("!HH", raw, ip_header_end)
        udp_len = struct.unpack_from("!H", raw, ip_header_end + 4)[0]
        payload_len = max(udp_len - 8, 0)
        local_port, remote_port = (sport, dport) if outbound else (dport, sport)
    elif proto in (PROTO_ICMP, PROTO_ICMPV6):
        local_port = remote_port = -1

    return PacketInfo(
        outbound=outbound,
        version=version,
        proto=proto,
        local_port=local_port,
        remote_port=remote_port,
        remote_addr=dst if outbound else src,
        size=size,
        payload_len=payload_len,
        tcp_flags=tcp_flags,
    )


def should_pass_through(info: PacketInfo, *, exempt_control: bool, limit_ipv6: bool) -> bool:
    """True when the packet must be reinjected immediately, without shaping."""
    if info.local_port < 0:
        # Fragment we cannot attribute to a process: never hold it back.
        return True
    if info.version == 6 and not limit_ipv6:
        return True
    if info.is_local_remote:
        return True
    if exempt_control and info.is_control:
        return True
    return False


def inspect_safe(raw: bytes | memoryview, outbound: bool) -> PacketInfo | None:
    """``inspect`` that never raises.

    Any parsing surprise returns ``None`` instead of killing the capture thread. A thread
    that dies while WinDivert holds packets means no internet at all, so the packet path
    must be incapable of propagating exceptions.
    """
    try:
        return inspect(raw, outbound)
    except Exception:  # noqa: BLE001 - deliberate catch-all on the hot path
        return None


#: Synthetic packets used by :func:`self_test` and by the tests (TCP from 51000 to 443).
SELF_TEST_IPV4 = bytes([
    0x45, 0x00, 0x00, 0x28, 0x00, 0x00, 0x00, 0x00, 0x40, 0x06, 0x00, 0x00,
    192, 168, 1, 10, 8, 8, 8, 8,
    0xC7, 0x38, 0x01, 0xBB, 0, 0, 0, 0, 0, 0, 0, 0, 0x50, 0x02, 0xFF, 0xFF, 0, 0, 0, 0,
])
SELF_TEST_IPV6 = bytes([
    0x60, 0x00, 0x00, 0x00, 0x00, 0x14, 0x06, 0x40,
    0xFE, 0x80] + [0] * 13 + [0x01] + [0x20, 0x01, 0x48, 0x60, 0x48, 0x60, 0, 0, 0, 0, 0, 0, 0, 0, 0x88, 0x88,
    0xC7, 0x38, 0x01, 0xBB, 0, 0, 0, 0, 0, 0, 0, 0, 0x50, 0x02, 0xFF, 0xFF, 0, 0, 0, 0,
])

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
                return False, (f"puertos incorrectos al analizar ipv{version}: "
                               f"{out.local_port} -> {out.remote_port}")
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
