from scripts import blob_store
import pytest

def test_object_key_is_content_addressed():
    digest = "a" * 64
    assert blob_store.object_key(digest, "bundle.tar.zst") == f"sha256/aa/{digest}/bundle.tar.zst"

def test_object_key_rejects_nested_filename():
    with pytest.raises(ValueError):
        blob_store.object_key("a" * 64, "../bundle")

def test_origin_must_be_https():
    with pytest.raises(ValueError):
        blob_store.public_url("http://example.invalid", "sha256/aa/x/file")
