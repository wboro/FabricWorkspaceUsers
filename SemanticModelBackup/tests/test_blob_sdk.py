"""Exercise real Azure SDK serialization with a local, non-network transport."""

from base64 import b64decode, b64encode
import hashlib
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree as ET

import semantic_model_backup as smb

try:
    from azure.core.pipeline.transport import HttpResponse, HttpTransport
    from azure.core.exceptions import ResourceExistsError
    from azure.storage.blob import BlobServiceClient

    HAS_AZURE = True
except ImportError:
    HAS_AZURE = False
    HttpResponse = object
    HttpTransport = object


class LocalDownload:
    def __init__(self, response):
        self.response = response
        self.content_length = len(response.data)

    def __iter__(self):
        yield self.response.data


class LocalResponse(HttpResponse):
    def __init__(self, request, status, headers=None, data=b""):
        super().__init__(request, None)
        self.status_code = status
        self.headers = headers or {}
        self.reason = "OK"
        self.content_type = "application/octet-stream"
        self.data = data

    def body(self):
        return self.data

    def stream_download(self, pipeline, **kwargs):
        return LocalDownload(self)


class LocalBlobTransport(HttpTransport):
    def __init__(self):
        self.calls = []
        self.blocks = {}
        self.committed = b""
        self.exists = False

    def open(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def send(self, request, **kwargs):
        if "etag" in kwargs or "match_condition" in kwargs:
            raise TypeError("Unconsumed conditional arguments reached the HTTP transport.")
        self.calls.append(request)
        query = parse_qs(urlparse(request.url).query)
        if request.method == "PUT":
            if (
                query.get("comp") != ["block"] and self.exists
                and request.headers.get("If-None-Match") == "*"
            ):
                return LocalResponse(request, 409, {"x-ms-error-code": "BlobAlreadyExists"})
            data = request.body
            if hasattr(data, "read"):
                data = data.read()
            if query.get("comp") == ["block"]:
                self.blocks[query["blockid"][0]] = data
            elif query.get("comp") == ["blocklist"]:
                root = ET.fromstring(data)
                self.committed = b"".join(self.blocks[node.text] for node in root)
            else:
                self.committed = data
            if query.get("comp") != ["block"]:
                self.exists = True
            return LocalResponse(request, 201, {"ETag": '"local-etag"'})
        if request.method == "HEAD":
            return LocalResponse(request, 200, {
                "ETag": '"local-etag"', "Content-Length": str(len(self.committed)),
                "x-ms-blob-type": "BlockBlob",
            })
        if request.method == "GET":
            byte_range = request.headers.get("x-ms-range", f"bytes=0-{len(self.committed) - 1}")
            start, end = (int(v) for v in byte_range.removeprefix("bytes=").split("-"))
            end = min(end, len(self.committed) - 1)
            data = self.committed[start:end + 1]
            return LocalResponse(request, 206, {
                "ETag": '"local-etag"', "Content-Length": str(len(data)),
                "Content-Range": f"bytes {start}-{end}/{len(self.committed)}",
                "Content-MD5": b64encode(hashlib.md5(data).digest()).decode(),
                "x-ms-blob-type": "BlockBlob",
            }, data)
        raise AssertionError(f"Unexpected SDK request {request.method}")


@unittest.skipUnless(HAS_AZURE, "Install Azure SDK requirements for transport-level tests")
class BlobSdkTests(unittest.TestCase):
    def setUp(self):
        block_size = patch.object(smb, "UPLOAD_BLOCK_SIZE", 128)
        block_size.start()
        self.addCleanup(block_size.stop)
        self.url = "https://archiveacct.blob.core.windows.net"
        self.transport = LocalBlobTransport()
        self.client = BlobServiceClient(
            self.url, transport=self.transport, max_block_size=128, max_single_put_size=128,
        )
        self.store = smb.BlobStore(None)
        self.store._clients[self.url] = self.client
        self.location = {"account_url": self.url, "container": "backups", "blob": "test.abf"}

    def tearDown(self):
        self.store.close()

    def test_iterable_upload_roundtrip_hash_and_conditional_download(self):
        data = b"native-backup" * 1000
        chunks = (data[i:i + 83] for i in range(0, len(data), 83))
        self.store.upload(self.location, chunks, len(data))
        self.assertEqual(self.transport.committed, data)
        writes = [r for r in self.transport.calls if r.method == "PUT"]
        self.assertGreater(len(writes), 1)
        self.assertEqual(writes[-1].headers.get("If-None-Match"), "*")
        for request in writes[:-1]:
            checksum = request.headers.get("Content-MD5")
            self.assertIsNotNone(checksum)
            self.assertEqual(b64decode(checksum), hashlib.md5(request.body).digest())
        properties = self.store.properties(self.location)
        self.assertEqual(properties, {"size": len(data), "etag": '"local-etag"'})
        self.assertEqual(
            smb.digest_blob(self.store, self.location, properties),
            hashlib.sha256(data).hexdigest(),
        )
        reads = [r for r in self.transport.calls if r.method == "GET"]
        self.assertTrue(reads)
        self.assertTrue(all(r.headers.get("If-Match") == '"local-etag"' for r in reads))

    def test_small_abf_uses_replayable_bytes_and_conditional_commit(self):
        data = b"small-abf" * 3
        self.store.upload(self.location, (data[i:i + 5] for i in range(0, len(data), 5)), len(data))
        writes = [r for r in self.transport.calls if r.method == "PUT"]
        self.assertEqual(len(writes), 2)
        self.assertEqual(parse_qs(urlparse(writes[0].url).query)["comp"], ["block"])
        self.assertIsInstance(writes[0].body, bytes)
        self.assertEqual(b64decode(writes[0].headers["Content-MD5"]), hashlib.md5(data).digest())
        self.assertEqual(writes[1].headers.get("If-None-Match"), "*")
        self.assertEqual(self.transport.committed, data)

    def test_large_input_chunk_is_split_into_bounded_upload_blocks(self):
        data = b"ABF" * 1000
        self.store.upload(self.location, iter([b"", data, b""]), len(data))
        writes = [
            r for r in self.transport.calls
            if parse_qs(urlparse(r.url).query).get("comp") == ["block"]
        ]
        self.assertGreater(len(writes), 1)
        self.assertTrue(all(isinstance(r.body, bytes) and len(r.body) <= 128 for r in writes))
        self.assertEqual(self.transport.committed, data)

    def test_truncated_or_oversized_stream_is_not_committed(self):
        for size in (255, 257):
            with self.subTest(size=size):
                before = len(self.transport.calls)
                with self.assertRaisesRegex(ValueError, "not committed"):
                    self.store.upload(self.location, iter([b"x" * 256]), size)
                writes = self.transport.calls[before:]
                self.assertFalse(any(
                    parse_qs(urlparse(r.url).query).get("comp") == ["blocklist"] for r in writes
                ))
                self.assertFalse(self.transport.exists)

    def test_existing_archive_is_not_overwritten_by_block_commit(self):
        self.store.upload(self.location, iter([b"original"]), 8)
        with self.assertRaises(ResourceExistsError):
            self.store.upload(self.location, iter([b"replacement"]), 11)
        self.assertEqual(self.transport.committed, b"original")

    def test_failed_source_iterator_is_not_committed(self):
        def chunks():
            yield b"x" * 128
            raise OSError("Source download failed")

        with self.assertRaisesRegex(OSError, "Source download failed"):
            self.store.upload(self.location, chunks(), 256)
        self.assertFalse(self.transport.exists)
        self.assertFalse(any(
            parse_qs(urlparse(r.url).query).get("comp") == ["blocklist"]
            for r in self.transport.calls
        ))

    def test_json_upload_is_immutable_and_has_content_type(self):
        document = {"schema_version": 1, "status": "succeeded"}
        self.store.put_json(self.location, document)
        request = self.transport.calls[-1]
        self.assertEqual(request.headers.get("If-None-Match"), "*")
        self.assertEqual(request.headers.get("x-ms-blob-content-type"), "application/json")
        self.assertEqual(self.store.get_json(self.location), document)


if __name__ == "__main__":
    unittest.main()
