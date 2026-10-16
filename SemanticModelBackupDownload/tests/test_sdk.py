"""Exercise actual Azure SDK upload serialization and conditional verification offline."""

from base64 import b64decode, b64encode
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import parse_qs
from xml.etree import ElementTree as ET

from azure.core.exceptions import ResourceExistsError
from azure.core.pipeline.transport import HttpResponse, HttpTransport
from azure.storage.blob import BlobServiceClient


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import semantic_model_backup_download as smb


class Download:
    def __init__(self, response):
        self.response = response
        self.data = response.data
        self.content_length = len(self.data)

    def __iter__(self):
        yield self.data


class Response(HttpResponse):
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
        return Download(self)


class Transport(HttpTransport):
    def __init__(self):
        self.calls = []
        self.blocks = {}
        self.data = None
        self.corrupt = False

    def open(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def send(self, request, **kwargs):
        self.calls.append(request)
        query = parse_qs(request.url.partition("?")[2])
        headers = {"ETag": '"etag"', "x-ms-blob-type": "BlockBlob"}
        if request.method == "PUT":
            operation = query.get("comp", [None])[0]
            if operation != "block" and self.data is not None:
                if request.headers.get("If-None-Match") == "*":
                    return Response(request, 409, {"x-ms-error-code": "BlobAlreadyExists"})
            data = request.body
            if hasattr(data, "read"):
                data = data.read()
            if operation == "block":
                self.blocks[query["blockid"][0]] = data
            elif operation == "blocklist":
                self.data = b"".join(self.blocks[node.text] for node in ET.fromstring(data))
            else:
                self.data = data
            return Response(request, 201, headers)
        if request.method == "GET":
            if query.get("restype") == ["container"]:
                return Response(request, 200, headers)
            data = self.data
            if self.corrupt:
                data = bytes([data[0] ^ 1]) + data[1:]
            headers.update({
                "Content-Length": str(len(data)),
                "Content-Range": f"bytes 0-{len(data) - 1}/{len(data)}",
                "Content-MD5": b64encode(hashlib.md5(data).digest()).decode(),
            })
            return Response(request, 206, headers, data)
        if request.method == "HEAD":
            return Response(request, 200, headers)
        raise AssertionError(f"Unexpected request: {request.method}")


class SdkTests(unittest.TestCase):
    def setUp(self):
        self.transport = Transport()
        self.service = BlobServiceClient(
            "https://archiveacct.blob.core.windows.net", transport=self.transport,
            max_single_put_size=128, max_block_size=128,
        )
        self.addCleanup(self.service.close)
        self.archive = smb.Archive.__new__(smb.Archive)
        self.archive.service = self.service
        self.archive.container = self.service.get_container_client("backups")
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "model.pbix"
        self.data = b"model-data" * 100
        self.path.write_bytes(self.data)
        self.expected = {
            "size": len(self.data), "sha256": hashlib.sha256(self.data).hexdigest(),
        }

    def test_block_upload_checksums_immutability_and_conditional_readback(self):
        self.archive.preflight()
        result = self.archive.upload("model.pbix", self.path, self.expected)
        self.assertEqual(result["sha256"], self.expected["sha256"])
        self.assertEqual(self.transport.data, self.data)
        writes = [r for r in self.transport.calls if r.method == "PUT"]
        self.assertGreater(len(writes), 2)
        self.assertEqual(writes[-1].headers["If-None-Match"], "*")
        for request in writes[:-1]:
            self.assertEqual(
                b64decode(request.headers["Content-MD5"]), hashlib.md5(request.body).digest(),
            )
        reads = [
            r for r in self.transport.calls
            if r.method == "GET" and "restype=container" not in r.url
        ]
        self.assertTrue(reads)
        self.assertTrue(all(r.headers["If-Match"] == '"etag"' for r in reads))
        with self.assertRaises(ResourceExistsError):
            self.archive.upload("model.pbix", self.path, self.expected)

    def test_corrupt_archive_is_not_success(self):
        self.transport.corrupt = True
        with self.assertRaisesRegex(smb.BackupError, "SHA-256"):
            self.archive.upload("model.pbix", self.path, self.expected)

    def test_small_upload_and_json_records_are_immutable(self):
        self.data = b"small-model"
        self.path.write_bytes(self.data)
        expected = {"size": len(self.data), "sha256": hashlib.sha256(self.data).hexdigest()}
        self.archive.upload("model.pbix", self.path, expected)
        self.assertEqual(self.transport.data, self.data)
        self.transport.data = None
        self.archive.json("manifest.json", {"status": "succeeded"})
        self.assertIn(b'"succeeded"', self.transport.data)
        with self.assertRaises(ResourceExistsError):
            self.archive.json("manifest.json", {"status": "succeeded"})


if __name__ == "__main__":
    unittest.main()
