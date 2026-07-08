"""Security validation for URLs and files."""

from __future__ import annotations

import contextlib
import ipaddress
import socket
from typing import Any

from src.config.server_config import ServerMode, cfg
from src.utils.error_handling import log_and_build_error


def _validate_url_security(url: str) -> tuple[bool, str]:
    """
    Validate URL for security risks to prevent SSRF attacks.

    Returns:
        (is_safe, error_message): True if safe, False with error message if unsafe
    """
    try:
        if not url or not url.strip():
            return False, "Empty URL not allowed"

        from urllib.parse import urlparse

        parsed = urlparse(url)
        config = cfg()

        if parsed.scheme == "http" and not config.allow_http_urls:
            return (
                False,
                "HTTP URLs not allowed (use HTTPS or enable allow_http_urls for development)",
            )

        if parsed.scheme not in ["http", "https"]:
            return False, f"Only HTTP/HTTPS URLs allowed, got: {parsed.scheme}"

        hostname = parsed.hostname
        if not hostname:
            return False, "Invalid URL: no hostname"

        localhost_variants = {
            "localhost",
            "127.0.0.1",
            "0.0.0.0",
            "::1",
            "127.1",
            "127.0.1",
        }
        if hostname.lower() in localhost_variants:
            return False, f"Localhost access blocked: {hostname}"

        if hostname.startswith("[") and hostname.endswith("]"):
            ipv6_addr = hostname[1:-1]
            if ipv6_addr.lower() in ["::1", "0:0:0:0:0:0:0:1"]:
                return False, f"Localhost access blocked: {hostname}"

        suspicious_domains = {
            "169.254.169.254",
            "metadata.google.internal",
        }

        for suspicious in suspicious_domains:
            if hostname == suspicious or hostname.startswith(suspicious):
                return False, f"Suspicious domain blocked: {hostname}"

        if config.block_private_ips:
            with contextlib.suppress(ValueError):
                ip = ipaddress.ip_address(hostname)
                if ip.is_private or ip.is_loopback or ip.is_link_local:
                    return False, f"Private IP access blocked: {hostname}"

        # Resolve hostname to IPs and validate each one.
        # This catches domains that resolve to loopback/private/link-local addresses.

        # Short timeout on DNS to prevent blocking on slow/unreachable nameservers.
        _original_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(5.0)
        try:
            try:
                addrinfo = socket.getaddrinfo(
                    hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM
                )
            except socket.gaierror:
                return False, f"DNS resolution failed for: {hostname}"
        finally:
            socket.setdefaulttimeout(_original_timeout)

        checked_ips: set[str] = set()
        for _family, _type, _proto, _canonname, sockaddr in addrinfo:
            ip_str = sockaddr[0]
            if ip_str in checked_ips:
                continue
            checked_ips.add(ip_str)

            if ipaddress.ip_address(ip_str).is_loopback:
                return (
                    False,
                    f"Loopback IP blocked after DNS resolution: {hostname} → {ip_str}",
                )

            if config.block_private_ips:
                ip_addr = ipaddress.ip_address(ip_str)
                if ip_addr.is_private or ip_addr.is_link_local:
                    return (
                        False,
                        f"Private/link-local IP blocked after DNS resolution: "
                        f"{hostname} → {ip_str}",
                    )
        return True, ""

    except Exception as e:
        return False, f"URL validation error: {e}"


def _validate_file_paths(
    files: str | list[str], operation: str, params: dict[str, Any]
) -> tuple[list[str] | None, dict[str, Any] | None]:
    """
    Normalize and validate file paths with security checks.

    Accepts:
    - data: URIs (base64 inline payloads) — all transport modes
    - http(s) URLs — all transport modes (with SSRF validation)
    - Local filesystem paths — stdio mode only

    Returns:
        (file_list, error): file_list if valid, error dict if validation fails
    """
    file_list = [files] if isinstance(files, str) else files
    config = cfg()

    for file in file_list:
        # data: URIs are accepted in all transport modes
        if file.startswith("data:"):
            try:
                from src.tools.messages.file_handling import _parse_data_uri

                _parse_data_uri(file)  # validate format and size
            except ValueError as exc:
                return None, log_and_build_error(
                    operation=operation,
                    error_message=f"Invalid data URI: {exc}",
                    params=params,
                    exception=ValueError(f"Data URI validation failed: {exc}"),
                )
            continue

        if (
            not file.startswith(("http://", "https://"))
            and config.server_mode != ServerMode.STDIO
        ):
            return None, log_and_build_error(
                operation=operation,
                error_message="Local file paths only supported in stdio mode",
                params=params,
                exception=ValueError("Local file paths require stdio mode"),
            )

        if file.startswith(("http://", "https://")):
            is_safe, error_msg = _validate_url_security(file)
            if not is_safe:
                return None, log_and_build_error(
                    operation=operation,
                    error_message=f"Unsafe URL blocked: {error_msg}",
                    params=params,
                    exception=ValueError(
                        f"URL security validation failed: {error_msg}"
                    ),
                )

    return file_list, None
