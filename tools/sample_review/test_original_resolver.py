#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import unittest

from tools.sample_review import original_resolver
from tools.sample_review.test_preview_resolver import PreviewResolverTests as _PreviewResolverTests


class OriginalResolverTests(_PreviewResolverTests):
    def test_original_is_exact_and_digest_bound(self) -> None:
        expected = self.path.read_bytes()
        result = original_resolver.resolve_review_original(
            self.connection,
            algorithm_key="takeaway_uniform",
            item_id="preview-a",
            review_revision=1,
            image_resolver=self._resolver,
        )
        self.assertEqual(result.body, expected)
        self.assertEqual(result.digest, hashlib.sha256(expected).hexdigest())
        self.assertEqual(result.headers["X-Content-SHA256"], result.digest)
        self.assertEqual(result.headers["Cache-Control"], "private, no-store")


if __name__ == "__main__":
    unittest.main()
