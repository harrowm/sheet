import sys


def test_python_is_314_or_newer():
    assert sys.version_info >= (3, 14)


def test_engine_package_importable_without_qt():
    import sheet.engine  # noqa: F401

    assert "PyQt6" not in sys.modules
