"""SimReady retrieval against an in-memory Hub: pinning, pagination and content verification."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.parse

from datafarm.adapters.assets.simready import (
    BOM, LICENSE, PACKAGING, ROOT_USDS, SimReadyClient, SimReadyError, verify_fetch,
)

COMMIT = "c" * 40
ASSET = "fixture_cabinet_x01"


class Response(io.BytesIO):
    def __init__(self, data: bytes, link: str = ""):
        super().__init__(data)
        self.headers = {"Link": link} if link else {}


class FakeHub:
    """Serves the subset of the Hub API the client uses; records every requested URL."""

    def __init__(self, files: dict[str, bytes], *, bom_override: bytes | None = None, pages=None):
        self.files = files
        honest = json.dumps({"items": [
            {"relative_path": k, "size": len(v), "hash": {"sha256": hashlib.sha256(v).hexdigest()}}
            for k, v in files.items()]}).encode()
        packaging = {"package_id": "pkg", "license": "CC-BY-4.0",
                     "metadata": [{"name": Path(BOM).name, "hash": {"sha256": hashlib.sha256(honest).hexdigest()}}]}
        self.meta = {
            PACKAGING: json.dumps(packaging).encode(),
            BOM: bom_override or honest,
            ROOT_USDS: json.dumps({"entries": ["simready_usd/a.usd"]}).encode(),
            LICENSE: b"CC-BY-4.0",
        }
        self.pages = pages or []
        self.urls: list[str] = []
        self.served: dict[str, bytes] = {}

    def __call__(self, request, timeout):
        url = request.full_url
        self.urls.append(url)
        path = urllib.parse.urlsplit(url).path
        if "/revision/" in path:
            return Response(json.dumps({"sha": COMMIT}).encode())
        if "/tree/" in path:
            query = urllib.parse.urlsplit(url).query
            index = int(urllib.parse.parse_qs(query).get("cursor", ["0"])[0])
            link = ""
            if index + 1 < len(self.pages):
                link = f'<https://huggingface.co{path}?limit=1000&cursor={index + 1}>; rel="next"'
            return Response(json.dumps(self.pages[index]).encode(), link)
        relative = urllib.parse.unquote(path.split(f"/resolve/{COMMIT}/{ASSET}/", 1)[1])
        data = self.meta.get(relative)
        if data is None:
            data = self.served.get(relative, self.files[relative])
        return Response(data)


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.files = {"simready_usd/a.usd": b"#usda 1.0\n", "simready_usd/Textures/t.png": b"png" * 10}

    def client(self, hub):
        return SimReadyClient(endpoint="https://hf-mirror.com", revision="main", opener=hub, retries=2)

    def test_fetch_pins_revision_and_verifies_every_file(self):
        hub = FakeHub(self.files)
        manifest = self.client(hub).fetch(ASSET, self.tmp)
        self.assertEqual(manifest["revision"], COMMIT)
        self.assertIn(f"/tree/{COMMIT}/{ASSET}", manifest["source_uri"])
        self.assertEqual({f["relative_path"] for f in manifest["files"]}, set(self.files))
        self.assertTrue(all(f"/resolve/{COMMIT}/" in u for u in hub.urls if "/resolve/" in u))
        self.assertEqual(verify_fetch(self.tmp / ASSET)["license"], "CC-BY-4.0")

    def test_corrupted_download_is_rejected_and_not_left_on_disk(self):
        hub = FakeHub(self.files)
        hub.served["simready_usd/a.usd"] = b"#usda 1.0\nTAMPERED"
        with self.assertRaises(SimReadyError):
            self.client(hub).fetch(ASSET, self.tmp)
        self.assertFalse((self.tmp / ASSET / "simready_usd/a.usd").exists())

    def test_bom_not_matching_packaging_manifest_is_rejected(self):
        forged = json.dumps({"items": [{"relative_path": "simready_usd/a.usd", "size": 1,
                                        "hash": {"sha256": "0" * 64}}]}).encode()
        with self.assertRaisesRegex(SimReadyError, "BOM digest"):
            self.client(FakeHub(self.files, bom_override=forged)).fetch(ASSET, self.tmp)

    def test_bom_path_escaping_the_asset_directory_is_rejected(self):
        files = {"../escape.txt": b"x"}
        hub = FakeHub(files)
        packaging = json.loads(hub.meta[PACKAGING])
        packaging["metadata"] = []
        hub.meta[PACKAGING] = json.dumps(packaging).encode()
        with self.assertRaisesRegex(SimReadyError, "unsafe"):
            self.client(hub).fetch(ASSET, self.tmp)
        self.assertFalse((self.tmp / "escape.txt").exists())

    def test_verify_detects_files_changed_after_fetch(self):
        self.client(FakeHub(self.files)).fetch(ASSET, self.tmp)
        (self.tmp / ASSET / "simready_usd/a.usd").write_bytes(b"edited")
        with self.assertRaises(SimReadyError):
            verify_fetch(self.tmp / ASSET)

    def test_listing_follows_pagination_on_the_configured_endpoint(self):
        pages = [[{"type": "directory", "path": "table_a01"}, {"type": "file", "path": "README.md"}],
                 [{"type": "directory", "path": "dishware_plate_a01"}]]
        hub = FakeHub(self.files, pages=pages)
        client = self.client(hub)
        client.revision = COMMIT
        self.assertEqual(client.list_assets(), ["dishware_plate_a01", "table_a01"])
        self.assertTrue(all(u.startswith("https://hf-mirror.com/") for u in hub.urls))


if __name__ == "__main__":
    unittest.main()
