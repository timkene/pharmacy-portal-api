"""The deployed Python runtime must stay pinned to the tested 3.11 line."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PINNED_MINOR = (3, 11)


def pinned_version():
    return (ROOT / ".python-version").read_text().strip()


def test_python_version_file_pins_a_full_311_release():
    version = pinned_version()
    assert re.fullmatch(r"3\.11\.\d+", version), version


def test_render_blueprint_does_not_override_the_pin():
    # Render's PYTHON_VERSION env var takes precedence over .python-version.
    render = (ROOT / "render.yaml").read_text()
    overrides = re.findall(r"key:\s*PYTHON_VERSION\s*\n\s*value:\s*\"?([\w.]+)\"?", render)
    assert overrides in ([], [pinned_version()]), overrides


def test_interpreter_meets_pinned_minimum():
    import sys
    # tomllib (catalog extractor) and PEP 604 unions require >= 3.11.
    assert sys.version_info[:2] >= PINNED_MINOR
