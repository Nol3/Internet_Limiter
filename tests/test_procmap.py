"""Process-mapping tests against real sockets owned by this test process."""
from __future__ import annotations

import os
import socket

import pytest

from limitador.procmap import ProcessMap, enum_endpoints, process_path


@pytest.mark.skipif(os.name != "nt", reason="Windows only")
def test_finds_a_listening_tcp_socket_of_this_process():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        endpoints = enum_endpoints()
        assert endpoints.get((6, port)) == os.getpid()
    finally:
        server.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows only")
def test_finds_a_bound_udp_socket():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        assert enum_endpoints().get((17, port)) == os.getpid()
    finally:
        sock.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows only")
def test_process_path_points_at_the_running_interpreter():
    path = process_path(os.getpid())
    assert path and path.lower().endswith((".exe",))
    assert "python" in path.lower()


@pytest.mark.skipif(os.name != "nt", reason="Windows only")
def test_process_map_resolves_name_and_pid():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    pmap = ProcessMap(poll_interval=10)
    try:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        pmap.refresh()
        assert pmap.pid_for(6, port) == os.getpid()
        assert "python" in pmap.name_for(os.getpid()).lower()
        assert pmap.port_count(os.getpid()) >= 1
        assert pmap.size > 0
    finally:
        server.close()


def test_socket_events_fill_gaps_the_poll_does_not_know():
    pmap = ProcessMap(poll_interval=10)
    pmap.refresh()
    pmap.note_socket_event(6, 59999, 4321)
    assert pmap.pid_for(6, 59999) == 4321
    assert pmap.pid_for(17, 59999) is None      # protocol must match
    assert pmap.pid_for(6, -1) is None
    pmap.note_socket_event(99, 1234, 4321)      # unsupported protocol ignored
    assert pmap.pid_for(99, 1234) is None
