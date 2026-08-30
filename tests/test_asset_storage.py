from __future__ import annotations

import unittest

from shared.asset_storage import AssetStorageClient


class StorageUriTests(unittest.TestCase):
    def test_download_gs_uri_uses_record_uri_and_infers_mp4_type(self):
        class Blob:
            content_type = None

            def exists(self): return True
            def download_as_bytes(self): return b"video"

        class Bucket:
            def __init__(self): self.path = None
            def blob(self, path):
                self.path = path
                return Blob()

        class Gcs:
            def __init__(self): self.bucket_name = None; self.bucket_value = Bucket()
            def bucket(self, name):
                self.bucket_name = name
                return self.bucket_value

        client = AssetStorageClient.__new__(AssetStorageClient)
        client._gcs = Gcs()
        data, content_type = client.download_gs_uri("gs://other-bucket/trailer/production/latest.mp4")

        self.assertEqual(data, b"video")
        self.assertEqual(content_type, "video/mp4")
        self.assertEqual(client._gcs.bucket_name, "other-bucket")
        self.assertEqual(client._gcs.bucket_value.path, "trailer/production/latest.mp4")
