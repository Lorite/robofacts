"""The license text ships with the distributions (Apache-2.0 requires it)."""
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]


def test_the_package_folder_holds_the_license_text():
    own = PACKAGE / "LICENSE"
    assert own.is_file(), "setuptools only packs a LICENSE file from the build folder"
    assert "Apache License" in own.read_text(encoding="utf-8")


def test_the_copy_is_identical_to_the_license_of_the_repository():
    root = PACKAGE.parent / "LICENSE"
    if not root.is_file():
        return  # an extracted sdist has no repository around it
    assert (PACKAGE / "LICENSE").read_bytes() == root.read_bytes()
