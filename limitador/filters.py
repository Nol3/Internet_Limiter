"""WinDivert filter construction and validation.

The filter language of WinDivert 2.2.2 has two rules that bite hard, both verified
against ``WinDivertHelperCompileFilter`` on this machine:

* ``not`` may **not** be followed by ``(``  -> ``not (ip)`` is a parse error.
  Negating a group must be written as ``(not a or not b)`` / ``(not a and not b)``.
* There are no CIDR operands and no ``!=`` on addresses: ranges are expressed with
  the plain comparison operators (``ip.SrcAddr >= 10.0.0.0``), which *are* valid.

Because of the first rule, "exclude these ranges" is written with De Morgan applied
by hand: ``NOT (lo <= x <= hi)`` becomes ``(not x >= lo or not x <= hi)``.
"""
from __future__ import annotations

from pydivert import WinDivert
from pydivert.consts import Layer

#: Ranges that must never be throttled. Only ever applied to the *remote* side of a
#: packet: for outbound traffic that is the destination, for inbound the source.
#: (The local side of every packet is a private address, so filtering it would
#: discard 100% of internet traffic - that was the v1 bug.)
DEFAULT_EXCLUDED_BLOCKS: tuple[tuple[str, str], ...] = (
    ("127.0.0.0", "127.255.255.255"),        # loopback
    ("10.0.0.0", "10.255.255.255"),          # RFC1918
    ("172.16.0.0", "172.31.255.255"),        # RFC1918
    ("192.168.0.0", "192.168.255.255"),      # RFC1918
    ("169.254.0.0", "169.254.255.255"),      # link-local (APIPA)
    ("224.0.0.0", "239.255.255.255"),        # multicast (mDNS/SSDP/UPnP)
    ("255.255.255.255", "255.255.255.255"),  # limited broadcast (DHCP renew)
)

#: IPv6 side: comparisons on 128-bit operands are not supported by the compiler, so
#: IPv6 local traffic is filtered in user space (see ``classify.is_local_remote``).
IPV6_EXCLUDED_PREFIXES: tuple[str, ...] = ("::1/128", "fe80::/10", "fc00::/7", "ff00::/8")


def _exclude_block_expr(field: str, lo: str, hi: str) -> str:
    """``NOT (lo <= field <= hi)`` without using ``not (...)``."""
    return f"(not {field} >= {lo} or not {field} <= {hi})"


def remote_field(direction: str) -> str:
    """The address field holding the *remote* party of a packet."""
    if direction == "outbound":
        return "ip.DstAddr"
    if direction == "inbound":
        return "ip.SrcAddr"
    raise ValueError(f"direction must be 'inbound' or 'outbound', got {direction!r}")


def build_network_filter(
    direction: str,
    *,
    exclude_lan: bool = True,
    excluded_blocks: tuple[tuple[str, str], ...] = DEFAULT_EXCLUDED_BLOCKS,
) -> str:
    """Kernel filter for one direction of the network layer.

    Matches the traffic that may be throttled: IPv4/IPv6 internet traffic, optionally
    excluding local networks, multicast and broadcast. IPv6 packets match the IPv4
    address conditions vacuously (``ip.DstAddr`` is false for them), which is what we
    want: they are captured and the per-family decision is made in user space.
    """
    parts = [direction, "not loopback"]
    if exclude_lan:
        field = remote_field(direction)
        parts.extend(_exclude_block_expr(field, lo, hi) for lo, hi in excluded_blocks)
    return " and ".join(parts)


def build_socket_filter() -> str:
    """Any socket-layer event: gives (protocol, local port) -> owning process."""
    return "true"


def validate_filter(filter_string: str, layer: Layer = Layer.NETWORK) -> tuple[bool, str]:
    """Compile a filter with WinDivert's own compiler (no driver needed).

    Returns ``(ok, message)``; on failure the message names the offending offset.
    """
    try:
        ok, pos, msg = WinDivert.check_filter(filter_string, layer)
    except Exception as exc:  # pragma: no cover - DLL missing / non-Windows
        return False, f"no se pudo validar el filtro: {exc}"
    if ok:
        return True, "ok"
    return False, f"{msg or 'filtro inválido'} (offset {pos}) en: {filter_string[max(0, pos - 20):pos + 20]!r}"
