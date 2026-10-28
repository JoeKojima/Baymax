"""User-triggered connection timing; no API key, conversation data or generation."""
import socket
import ssl
import time

HOST = 'generativelanguage.googleapis.com'


def probe():
    result = {'host': HOST, 'checked_at': time.time()}
    start = mark = time.perf_counter()
    stage = 'DNS'
    try:
        addresses = socket.getaddrinfo(HOST, 443, type=socket.SOCK_STREAM)
        result['dns_ms'] = round((time.perf_counter() - mark) * 1000, 1)
        stage = 'TCP'
        mark = time.perf_counter()
        last_error = OSError('No address')
        connection = None
        deadline = mark + 3
        for family, kind, protocol, _, address in addresses:
            candidate = socket.socket(family, kind, protocol)
            try:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    candidate.close()
                    break
                candidate.settimeout(remaining)
                candidate.connect(address)
                connection = candidate
                break
            except OSError as error:
                last_error = error
                candidate.close()
        if connection is None:
            raise last_error
        with connection:
            result['tcp_ms'] = round((time.perf_counter() - mark) * 1000, 1)
            stage = 'TLS'
            mark = time.perf_counter()
            connection.settimeout(3)
            with ssl.create_default_context().wrap_socket(connection, server_hostname=HOST):
                result['tls_ms'] = round((time.perf_counter() - mark) * 1000, 1)
        result['ok'] = True
    except (OSError, ssl.SSLError):
        result.update(ok=False, error=stage + ' connection check failed. Check connectivity and try again.')
    result['total_ms'] = round((time.perf_counter() - start) * 1000, 1)
    return result
