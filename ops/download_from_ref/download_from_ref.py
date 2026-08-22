# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import hashlib
import ipaddress
import mimetypes
import os
import pathlib
import socket
from dataclasses import fields
from tempfile import TemporaryDirectory
from typing import Any, Dict, Type, cast, get_origin
from urllib.parse import ParseResult, urlparse

import requests
from requests.adapters import HTTPAdapter

from vibe_core.data import (
    AssetVibe,
    DataVibe,
    ExternalReference,
    data_registry,
    gen_hash_id,
)
from vibe_core.file_downloader import download_file, retry_session
from vibe_core.uri import uri_to_filename

CHUNK_SIZE_BYTES = 1024 * 1024

ALLOWED_SCHEMES = ("http", "https")


def parse_external_url(url: str) -> ParseResult:
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES or not parsed.hostname:
        raise ValueError(
            f"Refusing to fetch reference {url!r}: only {'/'.join(ALLOWED_SCHEMES)} URLs "
            "are supported."
        )
    return parsed


def resolve_public_address(url: str) -> str:
    """Resolve an external URL to one validated public address."""
    parsed = parse_external_url(url)

    try:
        addresses = socket.getaddrinfo(parsed.hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError(f"Refusing to fetch reference {url!r}: could not resolve host") from e

    resolved = [ipaddress.ip_address(address[4][0]) for address in addresses]
    if not resolved:
        raise ValueError(f"Refusing to fetch reference {url!r}: host resolved to no addresses")
    for ip in resolved:
        if not ip.is_global or ip.is_multicast:
            raise ValueError(
                f"Refusing to fetch reference {url!r}: host resolves to non-public address {ip}."
            )
    return str(resolved[0])


class PinnedAddressAdapter(HTTPAdapter):
    """Connect to a validated address without resolving the hostname again."""

    def connection_for_url(self, url: str, proxies: Any = None) -> Any:
        if proxies:
            raise ValueError("Proxies are not supported for external references")

        parsed = urlparse(url)
        address = resolve_public_address(url)
        pool_kwargs = (
            {"assert_hostname": parsed.hostname, "server_hostname": parsed.hostname}
            if parsed.scheme == "https"
            else {}
        )
        return self.poolmanager.connection_from_host(
            address, port=parsed.port, scheme=parsed.scheme, pool_kwargs=pool_kwargs
        )

    def _get_connection(
        self, request: Any, verify: Any, proxies: Any = None, cert: Any = None
    ) -> Any:
        return self.connection_for_url(request.url, proxies)

    def get_connection_with_tls_context(
        self, request: Any, verify: Any, proxies: Any = None, cert: Any = None
    ) -> Any:
        return self.connection_for_url(request.url, proxies)

    def get_connection(self, url: str, proxies: Any = None) -> Any:
        return self.connection_for_url(url, proxies)

    def add_headers(self, request: Any, **kwargs: Any) -> None:
        super().add_headers(request, **kwargs)
        parsed = urlparse(request.url)
        host = cast(str, parsed.hostname)
        host = f"[{host}]" if ":" in host else host
        if parsed.port and parsed.port != {"http": 80, "https": 443}[parsed.scheme]:
            host = f"{host}:{parsed.port}"
        request.headers["Host"] = host


def pinned_session() -> requests.Session:
    adapter = PinnedAddressAdapter()
    session = retry_session(adapter)
    session.mount("", adapter)
    session.trust_env = False
    return session


def hash_file(filepath: str, chunk_size: int = CHUNK_SIZE_BYTES) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def get_empty_type(t: Any):
    o = get_origin(t)
    if o is not None:
        return o()
    return t()


def get_empty_fields(data_type: Type[DataVibe]) -> Dict[str, Any]:
    base_fields = [f for f in fields(DataVibe) if f.init]
    init_fields = [f for f in fields(data_type) if f.init and f not in base_fields]
    return {f.name: get_empty_type(f.type) for f in init_fields}


def add_mime_type(extension: str):
    if extension == ".geojson":
        mimetypes.add_type("application/json", ".geojson")


class CallbackBuilder:
    def __init__(self, out_type: str):
        self.tmp_dir = TemporaryDirectory()
        self.out_type = cast(Type[DataVibe], data_registry.retrieve(out_type))

    def __call__(self):
        def callback(input_ref: ExternalReference) -> Dict[str, DataVibe]:
            # Download the file
            parse_external_url(input_ref.url)
            out_path = os.path.join(self.tmp_dir.name, uri_to_filename(input_ref.url))
            with pinned_session() as session:
                download_file(input_ref.url, out_path, session=session)

            file_extension = pathlib.Path(out_path).suffix
            if file_extension not in mimetypes.types_map.keys():
                add_mime_type(file_extension)

            # Create asset and Raster
            asset_id = hash_file(out_path)
            asset = AssetVibe(
                reference=out_path, type=mimetypes.guess_type(out_path)[0], id=asset_id
            )
            out = self.out_type.clone_from(
                input_ref,
                id=gen_hash_id(asset_id, input_ref.geometry, input_ref.time_range),
                assets=[asset],
                **get_empty_fields(self.out_type),
            )
            return {"downloaded": out}

        return callback

    def __del__(self):
        self.tmp_dir.cleanup()
