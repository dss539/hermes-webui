"""Source guard for the owner-kept workspace-sized YOLO pill style."""
from pathlib import Path
import re


def test_yolo_pill_uses_workspace_sized_padding_and_no_tiny_uppercase_label():
    css = (Path(__file__).resolve().parents[1] / 'static/style.css').read_text()
    rule = re.search(r'\.yolo-pill\{([^}]+)\}', css).group(1)
    assert 'padding:8px 12px' in rule
    assert 'border-radius:999px' in rule
    assert 'font-weight:500' in rule
    assert not re.search(r'\.yolo-pill-label\{[^}]*text-transform:uppercase', css)
