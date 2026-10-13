"""Network helpers.

Kite validates order requests against the whitelisted static IP. Kite's API sits
behind Cloudflare, which also serves IPv6; if the server has an IPv6 route, Python
may connect over it and orders get rejected because only the IPv4 address is
registered. Forcing IPv4 for every outbound connection (REST and WebSocket) keeps
the source address the one Kite knows.
"""
import socket

_original_getaddrinfo = socket.getaddrinfo


def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if family in (0, socket.AF_UNSPEC):
        family = socket.AF_INET
    return _original_getaddrinfo(host, port, family, type, proto, flags)


def force_ipv4():
    socket.getaddrinfo = _ipv4_getaddrinfo
