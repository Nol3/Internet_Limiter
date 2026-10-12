"""Filter construction tests.

Two levels of verification, both against the real WinDivert DLL (no admin needed):

* the filter must **compile** (``WinDivertHelperCompileFilter``);
* the filter must **mean** what we think (``WinDivertHelperEvalFilter`` on synthetic
  packets). This is what v1 never checked, and it is how both original bugs were found.
"""

from __future__ import annotations

import ctypes
import struct

import pytest
from pydivert import windivert_dll
from pydivert.consts import Layer
from pydivert.windivert_dll import WinDivertAddress

from limitador.filters import (
    build_flow_filter,
    build_network_filter,
    build_socket_filter,
    port_clause,
    validate_filter,
)


def ipv4_tcp(src: str, dst: str, sport: int = 50000, dport: int = 443, payload: int = 0) -> bytes:
    ip = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        40 + payload,
        0,
        0,
        64,
        6,
        0,
        bytes(int(o) for o in src.split(".")),
        bytes(int(o) for o in dst.split(".")),
    )
    tcp = struct.pack("!HHIIBBHHH", sport, dport, 0, 0, 0x50, 0x02, 65535, 0, 0)
    return ip + tcp + b"\x00" * payload


def evaluates_to(filter_string: str, raw: bytes, outbound: bool) -> bool:
    address = WinDivertAddress()
    address.Layer = int(Layer.NETWORK)
    address.Outbound = 1 if outbound else 0
    address.u.Network.IfIdx = 1
    buffer = (ctypes.c_char * len(raw)).from_buffer_copy(raw)
    return bool(
        windivert_dll.WinDivertHelperEvalFilter(
            filter_string.encode(), buffer, len(raw), ctypes.byref(address)
        )
    )


@pytest.mark.parametrize("direction", ["inbound", "outbound"])
def test_filter_compiles(direction):
    for exclude_lan in (True, False):
        ok, message = validate_filter(build_network_filter(direction, exclude_lan=exclude_lan))
        assert ok, message


def test_filter_avoids_the_illegal_not_parenthesis():
    """Regression guard for the v1 bug: 'not (' is a parse error in WinDivert 2.2.2."""
    filt = build_network_filter("outbound")
    assert "not (" not in filt
    ok, _ = validate_filter("outbound and not (ip)")
    assert not ok, "WinDivert unexpectedly accepted 'not ('"


OUTBOUND_CASES = [
    # remote side is the destination: only it decides local/remote
    ("192.168.1.10", "8.8.8.8", True),
    ("192.168.1.10", "142.250.184.78", True),
    ("10.0.0.1", "1.1.1.1", True),  # own LAN IP must NOT exclude the packet
    ("192.168.1.10", "192.168.1.20", False),
    ("192.168.1.10", "10.5.5.5", False),
    ("192.168.1.10", "172.16.0.1", False),
    ("192.168.1.10", "172.31.255.254", False),
    ("192.168.1.10", "172.32.0.1", True),  # just outside 172.16/12
    ("192.168.1.10", "169.254.9.9", False),
    ("192.168.1.10", "239.255.255.250", False),
    ("192.168.1.10", "255.255.255.255", False),
    ("192.168.1.10", "9.255.255.255", True),
    ("192.168.1.10", "11.0.0.1", True),
]


@pytest.mark.parametrize("src,dst,expected", OUTBOUND_CASES)
def test_outbound_only_excludes_remote_local_networks(src, dst, expected):
    filt = build_network_filter("outbound")
    assert evaluates_to(filt, ipv4_tcp(src, dst), outbound=True) is expected


INBOUND_CASES = [
    ("8.8.8.8", "192.168.1.100", True),
    ("142.250.184.78", "10.0.0.5", True),  # own private IP must not exclude
    ("192.168.1.5", "192.168.1.100", False),
    ("224.0.0.251", "192.168.1.100", False),
]


@pytest.mark.parametrize("src,dst,expected", INBOUND_CASES)
def test_inbound_only_excludes_remote_local_networks(src, dst, expected):
    filt = build_network_filter("inbound")
    assert evaluates_to(filt, ipv4_tcp(src, dst), outbound=False) is expected


def test_loopback_never_matches():
    filt = build_network_filter("outbound")
    raw = ipv4_tcp("127.0.0.1", "127.0.0.1", 50000, 8080)
    assert evaluates_to(filt, raw, outbound=True) is False


def test_disabling_lan_exclusion_captures_local_traffic_too():
    filt = build_network_filter("outbound", exclude_lan=False)
    assert evaluates_to(filt, ipv4_tcp("192.168.1.10", "192.168.1.20"), outbound=True) is True


def test_capture_filter_leaves_out_our_own_injected_packets():
    """Con descriptores separados para capturar e inyectar, lo inyectado no debe recapturarse.

    Se apoya en la marca *impostor* del driver. Sin ella, cada paquete reinyectado volvería a
    entrar por el descriptor de captura: un bucle.
    """
    for direction in ("outbound", "inbound"):
        filt = build_network_filter(direction, exclude_own=True)
        assert "not impostor" in filt
        ok, message = validate_filter(filt)
        assert ok, message
    # Por defecto no se excluye nada: con un solo descriptor el driver ya evita la recaptura.
    assert "not impostor" not in build_network_filter("outbound")


def test_event_layer_filters_compile_for_their_own_layers():
    """La atribución por proceso depende de que estos filtros compilen en su capa.

    Se comprueba con el compilador de WinDivert (sin driver ni administrador), que es
    justo lo que la v1 nunca hizo con su filtro de red.
    """
    for filter_string, layer in (
        (build_flow_filter(), Layer.FLOW),
        (build_socket_filter(), Layer.SOCKET),
    ):
        ok, message = validate_filter(filter_string, layer)
        assert ok, f"{layer.name}: {message}"


def test_port_scope_filters_compile():
    """Las cláusulas de puertos tienen que compilar con el compilador del driver."""
    for direction in ("inbound", "outbound"):
        for kwargs in (
            {"only_ports": (443, 8443)},
            {"exclude_ports": (51000, 51001)},
            {"only_ports": (443,), "exclude_ports": (51000,)},
        ):
            ok, message = validate_filter(build_network_filter(direction, **kwargs))
            assert ok, f"{kwargs}: {message}"


def test_capture_nothing_filter_compiles():
    """El alcance "limited" sin nada que limitar usa ``false``: tiene que ser un filtro válido."""
    ok, message = validate_filter("false")
    assert ok, message


def test_only_ports_captures_just_that_traffic():
    """Con solo estos puertos, el tráfico de otros puertos no entra en el motor."""
    filt = build_network_filter("outbound", exclude_lan=False, only_ports=(443,))
    assert evaluates_to(filt, ipv4_tcp("192.168.1.10", "8.8.8.8", dport=443), True) is True
    assert evaluates_to(filt, ipv4_tcp("192.168.1.10", "8.8.8.8", dport=80), True) is False


def test_excluded_ports_leave_that_traffic_alone():
    """Los puertos con prioridad no se capturan: su tráfico no puede ser retrasado."""
    filt = build_network_filter("outbound", exclude_lan=False, exclude_ports=(51000,))
    assert evaluates_to(filt, ipv4_tcp("192.168.1.10", "8.8.8.8", sport=51000), True) is False
    assert evaluates_to(filt, ipv4_tcp("192.168.1.10", "8.8.8.8", sport=52000), True) is True


def test_port_negation_avoids_not_parenthesis_and_guards_protocols():
    """La exclusión de puertos se escribe con De Morgan y con guardas de protocolo.

    Sin las guardas, ``not tcp.SrcPort == X`` descarta también paquetes de otros protocolos
    (el campo vale 0 y la negación se evalúa como ``(not 0) == X``). Medido con el evaluador
    del driver; este test lo fija para que no vuelva.
    """
    clause = port_clause((51000, 51001), negate=True)
    assert "not (" not in clause
    assert "(not tcp or (" in clause and "(not udp or (" in clause
    assert port_clause((), negate=True) == "true"


def test_port_exclusion_behaves_as_expected_per_protocol():
    """Lo que de verdad hace la cláusula, protocolo a protocolo (con el evaluador del driver)."""
    filt = build_network_filter("outbound", exclude_lan=False, exclude_ports=(51000,))

    def udp(sport, dport=443):
        ip = struct.pack(
            "!BBHHHBBH4s4s",
            0x45,
            0,
            28,
            0,
            0,
            64,
            17,
            0,
            bytes([192, 168, 1, 10]),
            bytes([8, 8, 8, 8]),
        )
        return ip + struct.pack("!HHHH", sport, dport, 8, 0)

    def icmp():
        ip = struct.pack(
            "!BBHHHBBH4s4s",
            0x45,
            0,
            28,
            0,
            0,
            64,
            1,
            0,
            bytes([192, 168, 1, 10]),
            bytes([8, 8, 8, 8]),
        )
        return ip + struct.pack("!BBHHH", 8, 0, 0, 1, 1)

    assert evaluates_to(filt, ipv4_tcp("192.168.1.10", "8.8.8.8", sport=51000), True) is False
    assert evaluates_to(filt, ipv4_tcp("192.168.1.10", "8.8.8.8", sport=52000), True) is True
    assert evaluates_to(filt, udp(51000), True) is False
    assert evaluates_to(filt, udp(52000), True) is True
    assert evaluates_to(filt, icmp(), True) is True, "ICMP no tiene puertos: no se excluye"
