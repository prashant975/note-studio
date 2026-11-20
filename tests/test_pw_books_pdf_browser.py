"""Mac PDF conversion retains text, equations, pictures, and existing exports on failure."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT/'engines/pw_books'


def adapter():
    spec = importlib.util.spec_from_file_location('pw_books_pdf_browser', ENGINE/'src/pdf_browser.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_pandoc_keeps_previous_export(tmp_path, monkeypatch):
    module = adapter()
    monkeypatch.setattr(module, 'find_pandoc', lambda: None)
    previous = tmp_path/'notes.pdf'
    previous.write_bytes(b'existing export')
    result, error = module.export_pdf(tmp_path/'notes.docx', tmp_path)
    assert result is None and 'Pandoc' in error
    assert previous.read_bytes() == b'existing export'


def test_invalid_explicit_browser_fails_clearly(monkeypatch, tmp_path):
    monkeypatch.setenv('CHROMIUM_PATH', str(tmp_path/'missing Chrome'))
    with pytest.raises(RuntimeError, match='does not exist'):
        adapter().find_browser()


@pytest.mark.skipif(sys.platform != 'darwin', reason='Native Mac Chrome PDF path')
def test_mac_real_docx_math_table_image_and_spaces(tmp_path):
    python = ENGINE/'.venv/bin/python'
    if not python.is_file() or not adapter().find_pandoc():
        pytest.skip('Install local PW Books environment and Pandoc first')
    code = r'''
from pathlib import Path
import sys
from docx import Document
from PIL import Image
import fitz
from src.math_renderer import add_block_math
from src.pdf_exporter import export_docx_to_pdf
work = Path(sys.argv[1])
source = work/'notes with spaces.docx'
image = work/'synthetic diagram.png'
Image.new('RGB', (80, 40), 'blue').save(image)
doc = Document()
doc.add_paragraph('Prime number export verification')
doc.add_paragraph('हिंदी पाठ')
add_block_math(doc, r'\frac{x^2}{2} = 5')
table = doc.add_table(rows=2, cols=2)
table.cell(0,0).text = 'Quantity'
table.cell(1,0).text = 'Mass'
table.cell(1,1).text = '10 kg'
doc.add_picture(str(image))
doc.save(source)
pdf, error = export_docx_to_pdf(source, work/'output with spaces')
assert pdf, error
with fitz.open(pdf) as result:
    text = ''.join(page.get_text() for page in result)
    assert 'Prime number' in text and 'Mass' in text and '10 kg' in text, text
    assert 'x' in text and '5' in text, text
    assert any(page.get_images() for page in result), 'Picture was lost'
    assert result[0].rect.width > 500
print('BROWSER_PDF_OK')
'''
    result = subprocess.run([str(python), '-c', code, str(tmp_path)], cwd=ENGINE,
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout+result.stderr
    assert 'BROWSER_PDF_OK' in result.stdout
