#!/usr/bin/env python3
import hashlib,json,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parent
PACKAGES=("ai_bot_review","ai_bot_capture","ai_bot_regression","ai_bot_visual_semantics","ai_bot_lineage","ai_bot_validation")
class PackageTests(unittest.TestCase):
    def test_manifests_bind_every_declared_file_and_presentation(self):
        for name in PACKAGES:
            root=ROOT/name;manifest=json.loads((root/"manifest.json").read_text(encoding="utf-8"));self.assertEqual(manifest["connector_key"],name)
            expected_pair=("image_collection","image_grid") if name=="ai_bot_capture" else ("structured_records","record_samples")
            self.assertEqual((manifest["content_kind"],manifest["preview_mode"]),expected_pair)
            hasher=hashlib.sha256()
            for member in manifest["files"]:
                payload=(root/member["path"]).read_bytes();digest=hashlib.sha256(payload).hexdigest();self.assertEqual(member["sha256"],digest)
                hasher.update(f"{member['path']}\n{digest}\n{member['role']}\n".encode())
            self.assertEqual(manifest["source_revision"],hasher.hexdigest())
    def test_packages_contain_no_binary_secret_database_model_or_host_path(self):
        forbidden=(b"BEGIN PRIVATE KEY",b"api-keys.vault",b"/srv/",b"C:\\Users\\",b".sqlite3",b".rknn",b".onnx")
        for name in PACKAGES:
            root=ROOT/name;manifest=json.loads((root/"manifest.json").read_text(encoding="utf-8"))
            for member in manifest["files"]:
                payload=(root/member["path"]).read_bytes();self.assertFalse(any(value in payload for value in forbidden),f"{name}/{member['path']}")
                self.assertNotIn(b"\x00",payload)
if __name__=="__main__":unittest.main()
