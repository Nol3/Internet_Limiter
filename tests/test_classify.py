"""Packet classification tests: parsing, control-traffic detection, pass-through."""

from __future__ import annotations

import ipaddress
import struct

import pytest

from limitador.classify import (
    inspect,
    inspect_safe,
    is_local_remote,
    is_local_v4_int,
    self_test,
    should_pass_through,
)

TCP_NO_PAYLOAD = 0x10


def ipv4(src: str, dst: str, proto: int, body: bytes, frag_offset: int = 0) -> bytes:
    total = 20 + len(body)
    flags_offset = frag_offset & 0x1FFF
    return (
        struct.pack(
            "!BBHHHBBH4s4s",
            0x45,
            0,
            total,
            0,
            flags_offset,
            64,
            proto,
            0,
            ipaddress.IPv4Address(src).packed,
            ipaddress.IPv4Address(dst).packed,
        )
        + body
    )


def ipv6(src: str, dst: str, proto: int, body: bytes) -> bytes:
    return (
        struct.pack(
            "!IHBB16s16s",
            6 << 28,
            len(body),
            proto,
            64,
            ipaddress.IPv6Address(src).packed,
            ipaddress.IPv6Address(dst).packed,
        )
        + body
    )


def tcp(sport: int, dport: int, payload: int = 0, flags: int = 0x02) -> bytes:
    return struct.pack("!HHIIBBHHH", sport, dport, 0, 0, 0x50, flags, 65535, 0, 0) + b"x" * payload


def udp(sport: int, dport: int, payload: int = 0) -> bytes:
    return struct.pack("!HHHH", sport, dport, 8 + payload, 0) + b"y" * payload


def test_parses_ipv4_tcp_outbound():
    info = inspect(ipv4("192.168.1.10", "8.8.8.8", 6, tcp(51000, 443, payload=100)), outbound=True)
    assert info is not None
    assert (info.version, info.proto, info.local_port, info.remote_port) == (4, 6, 51000, 443)
    assert info.remote_addr == "8.8.8.8"
    assert info.payload_len == 100
    assert info.outbound is True


def test_parses_ipv4_tcp_inbound_swaps_ports():
    info = inspect(ipv4("8.8.8.8", "192.168.1.10", 6, tcp(443, 51000)), outbound=False)
    assert info is not None
    assert (info.local_port, info.remote_port) == (51000, 443)
    assert info.remote_addr == "8.8.8.8"


def test_parses_ipv6_udp():
    info = inspect(
        ipv6("2001:4860:4860::8888", "fe80::1", 17, udp(53, 40000, payload=20)), outbound=False
    )
    assert info is not None
    assert info.version == 6 and info.proto == 17
    assert (info.local_port, info.remote_port) == (40000, 53)
    assert info.remote_addr == "2001:4860:4860::8888"


def test_parses_ipv6_with_extension_header():
    body = bytes([6, 0, 0, 0, 0, 0, 0, 0]) + tcp(51000, 443, payload=50)  # hop-by-hop + TCP
    info = inspect(ipv6("2001:4860:4860::8888", "fe80::1", 0, body), outbound=True)
    assert info is not None
    assert (info.proto, info.local_port, info.remote_port) == (6, 51000, 443)
    assert info.payload_len == 50


def test_fragment_without_transport_header_is_not_attributable():
    info = inspect(
        ipv4("192.168.1.10", "8.8.8.8", 6, tcp(51000, 443), frag_offset=185), outbound=True
    )
    assert info is not None
    assert info.local_port == -1
    assert should_pass_through(info, exempt_control=True, limit_ipv6=True) is True


def test_short_or_unknown_frames_are_ignored():
    assert inspect(b"\x00" * 10, outbound=True) is None
    assert inspect(b"\x30" + b"\x00" * 30, outbound=True) is None


@pytest.mark.parametrize("wrapper", [memoryview, bytearray, bytes])
def test_buffers_are_parsed_the_same_way(wrapper):
    """Regression test for the outage: pydivert hands packets over as memoryview, and
    slicing a memoryview returns another memoryview, which IPv4Address cannot parse."""
    raw = ipv4("192.168.1.10", "8.8.8.8", 6, tcp(51000, 443, payload=100))
    info = inspect(wrapper(raw), outbound=True)
    assert info is not None
    assert info.remote_addr == "8.8.8.8"
    assert info.local_port == 51000


def test_memoryview_fragments_and_ipv6_also_parse():
    raw6 = ipv6("fe80::1", "2001:4860:4860::8888", 6, tcp(51000, 443, payload=10))
    info = inspect(memoryview(raw6), outbound=True)
    assert info is not None and info.remote_addr == "2001:4860:4860::8888"


def test_inspect_safe_never_raises():
    for raw in (b"", b"\x45", b"\x00" * 8, b"\xff" * 64, b"\x60" * 40):
        assert inspect_safe(memoryview(raw), outbound=True) is None or True
    assert inspect_safe(memoryview(b""), True) is None
    assert inspect_safe(memoryview(b"\x45"), True) is None


def test_engine_self_test_passes():
    ok, detail = self_test()
    assert ok, detail


@pytest.mark.parametrize("payload,expected", [(0, True), (1, False), (1400, False)])
def test_pure_ack_detection(payload, expected):
    info = inspect(
        ipv4("192.168.1.10", "8.8.8.8", 6, tcp(51000, 443, payload, flags=TCP_NO_PAYLOAD)),
        outbound=True,
    )
    assert info.is_pure_ack is expected
    assert info.is_control is expected


def test_dns_and_icmp_count_as_control():
    dns = inspect(ipv4("192.168.1.10", "8.8.8.8", 17, udp(51000, 53, payload=30)), outbound=True)
    assert dns.is_control is True
    icmp = inspect(
        ipv4("192.168.1.10", "8.8.8.8", 1, b"\x08\x00\x00\x00" + b"\x00" * 28), outbound=True
    )
    assert icmp.is_control is True and icmp.local_port == -1


def test_control_exemption_can_be_turned_off():
    info = inspect(
        ipv4("192.168.1.10", "8.8.8.8", 6, tcp(51000, 443, 0, TCP_NO_PAYLOAD)), outbound=True
    )
    assert should_pass_through(info, exempt_control=True, limit_ipv6=True) is True
    assert should_pass_through(info, exempt_control=False, limit_ipv6=True) is False


@pytest.mark.parametrize(
    "addr,expected",
    [
        ("8.8.8.8", False),
        ("1.1.1.1", False),
        ("192.168.1.5", True),
        ("10.0.0.1", True),
        ("172.20.0.1", True),
        ("172.40.0.1", False),
        ("169.254.1.1", True),
        ("239.255.255.250", True),
        ("255.255.255.255", True),
        ("::1", True),
        ("fe80::1", True),
        ("fd00::1", True),
        ("ff02::1", True),
        ("2001:4860:4860::8888", False),
    ],
)
def test_local_remote_detection(addr, expected):
    assert is_local_remote(addr) is expected


def test_local_traffic_is_never_queued():
    lan = inspect(
        ipv4("192.168.1.10", "192.168.1.20", 6, tcp(51000, 445, payload=500)), outbound=True
    )
    assert lan.is_local_remote is True
    assert should_pass_through(lan, exempt_control=True, limit_ipv6=True) is True


def test_ipv6_can_be_excluded_from_limiting():
    # Local side is link-local (fe80::), remote side is a public address.
    info = inspect(
        ipv6("fe80::1", "2001:4860:4860::8888", 6, tcp(51000, 443, payload=100)), outbound=True
    )
    assert should_pass_through(info, exempt_control=True, limit_ipv6=False) is True
    assert should_pass_through(info, exempt_control=True, limit_ipv6=True) is False


def test_fast_lan_check_matches_the_reference():
    """El atajo en enteros y la comprobación con ``ipaddress`` tienen que coincidir.

    El camino por paquete usa el atajo (sin construir objetos) y el filtro del kernel usa los
    rangos: si divergieran, el limitador trataría como "internet" tráfico que es local (o al
    revés), así que se comprueban los bordes de cada rango.
    """
    samples = [
        "0.0.0.0",
        "1.1.1.1",
        "8.8.8.8",
        "10.0.0.1",
        "10.255.255.255",
        "11.0.0.1",
        "127.0.0.1",
        "127.255.255.255",
        "169.254.1.1",
        "172.15.255.255",
        "172.16.0.1",
        "172.31.255.255",
        "172.32.0.1",
        "192.167.255.255",
        "192.168.1.10",
        "192.168.255.255",
        "192.169.0.1",
        "223.255.255.255",
        "224.0.0.1",
        "239.255.255.255",
        "240.0.0.1",
        "255.255.255.255",
    ]
    for text in samples:
        value = int(ipaddress.IPv4Address(text))
        assert is_local_v4_int(value) is is_local_remote(text), text


def tcp_bytes(sport: int = 51000, dport: int = 443, payload: bytes = b"") -> bytes:
    return (
        struct.pack("!HHIIBBHHH", sport, dport, 0, 0, (5 << 4), TCP_NO_PAYLOAD, 8192, 0, 0)
        + payload
    )


def test_parsed_packet_answers_local_check_without_formatting():
    """El paquete resuelve "¿es local?" con el entero, y el texto solo se calcula al pedirlo."""
    # Entrante: la dirección remota es el ORIGEN (8.8.8.8), no el destino.
    info = inspect(ipv4("8.8.8.8", "192.168.1.10", 6, tcp_bytes()), outbound=False)
    assert info is not None
    assert info.is_local_remote is False
    assert info._remote_text is None, "no debe formatear la dirección si nadie la pide"
    assert info.remote_addr == "8.8.8.8"
    assert info._remote_text == "8.8.8.8"

    # Y al revés: si el origen es de la LAN, la remota es local.
    lan = inspect(ipv4("192.168.1.10", "8.8.8.8", 6, tcp_bytes()), outbound=False)
    assert lan is not None and lan.is_local_remote is True
