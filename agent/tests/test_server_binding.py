import errno
import socket

import pytest

from agent.dev.test_server import LocalHTTPServer


def assert_address_occupied(error):
    # Windows reports either WSAEACCES or WSAEADDRINUSE for an exclusive port.
    assert error.errno in {errno.EACCES, errno.EADDRINUSE, 10013, 10048}


def test_second_console_cannot_share_the_existing_listener():
    with LocalHTTPServer(("127.0.0.1", 0), None, None) as first:
        with pytest.raises(OSError) as exc:
            LocalHTTPServer(first.server_address, None, None)
        assert_address_occupied(exc.value)


def test_legacy_reuse_address_listener_cannot_share_the_console_port():
    with LocalHTTPServer(("127.0.0.1", 0), None, None) as first:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as legacy:
            legacy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with pytest.raises(OSError) as exc:
                legacy.bind(first.server_address)
                legacy.listen()
            assert_address_occupied(exc.value)


@pytest.mark.skipif(not hasattr(socket, "SO_EXCLUSIVEADDRUSE"), reason="Windows socket option")
def test_windows_listener_claims_exclusive_address_use():
    with LocalHTTPServer(("127.0.0.1", 0), None, None) as server:
        assert server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE) == 1
        assert server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 0


@pytest.mark.parametrize("host", ["0.0.0.0", "localhost", "::1"])
def test_console_still_rejects_nonexplicit_loopback_addresses(host):
    with pytest.raises(ValueError, match="127.0.0.1"):
        LocalHTTPServer((host, 0), None, None)
