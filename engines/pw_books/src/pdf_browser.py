"""Local DOCX -> HTML/MathML -> Chrome PDF, without an Office installation."""
from __future__ import annotations

from html import escape
import base64
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def find_pandoc():
    return shutil.which('pandoc') or next((str(p) for p in (
        Path.home()/'.local/bin/pandoc', Path('/opt/homebrew/bin/pandoc'),
        Path('/usr/local/bin/pandoc')) if p.is_file()), None)


def find_browser():
    override = os.environ.get('CHROMIUM_PATH') or os.environ.get('PW_BOOKS_CHROME_EXE')
    if override:
        path = Path(override).expanduser()
        if not path.is_file():
            raise RuntimeError('The configured Chrome executable does not exist.')
        return str(path)
    for folder in (Path('/Applications'), Path.home()/'Applications'):
        path = folder/'Google Chrome.app/Contents/MacOS/Google Chrome'
        if path.is_file():
            return str(path)
    return None  # Playwright-managed Chromium when installed


def export_pdf(source: Path, output_dir: Path):
    """Generate offline, validate, then atomically replace the destination PDF."""
    try:
        from docx import Document
        from docx.oxml.ns import qn
        import fitz
        from playwright.sync_api import sync_playwright
        pandoc = find_pandoc()
        if not pandoc:
            return None, 'PDF export needs Pandoc and Chrome. Install Pandoc, then retry.'
        source, output_dir = Path(source).resolve(), Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        document = Document(source)
        section = document.sections[0]
        header = ' · '.join(p.text for p in section.header.paragraphs if p.text.strip())
        footer = ' · '.join(p.text for p in section.footer.paragraphs if p.text.strip())
        header_images = ''
        for blip in section.header._element.xpath('.//a:blip'):
            part = section.header.part.related_parts.get(blip.get(qn('r:embed')))
            if part is not None:
                encoded = base64.b64encode(part.blob).decode('ascii')
                header_images += f'<img style="height:24px" src="data:{part.content_type};base64,{encoded}">'
        with tempfile.TemporaryDirectory(prefix='.browser-pdf-', dir=output_dir) as folder:
            work = Path(folder)
            html = work/'notes.html'
            subprocess.run([pandoc, str(source), '--from=docx', '--to=html5', '--standalone',
                            '--embed-resources', '--mathml', '--extract-media='+str(work/'media'),
                            '--output='+str(html)], check=True, capture_output=True, text=True,
                           timeout=120, cwd=work)
            css = '''body{font-family:Kalam,"Noto Sans Devanagari",sans-serif;font-size:12pt;
                line-height:1.45;max-width:none;margin:0;padding:0;color:#292929}
                table{border-collapse:collapse;width:100%;margin:8pt 0}
                td,th{border:1px solid #888;padding:5pt;vertical-align:top}
                tr,img{break-inside:avoid}img{max-width:100%;height:auto}
                h1,h2,h3{break-after:avoid;color:#22538a}math{font-family:math}
                pre{white-space:pre-wrap}'''
            rendered = work/(source.stem+'.pdf')
            with sync_playwright() as tools:
                browser = tools.chromium.launch(headless=True, executable_path=find_browser())
                try:
                    page = browser.new_page()
                    # Rendering never uploads a document or fetches remote resources.
                    page.route('**/*', lambda route: route.continue_()
                               if route.request.url.startswith(('file:', 'data:')) else route.abort())
                    page.goto(html.as_uri(), wait_until='load')
                    page.add_style_tag(content=css)
                    page.evaluate('document.fonts.ready')
                    page.evaluate('Promise.all(Array.from(document.images).map(i => i.decode()))')
                    furniture = lambda text: '<div style="font-size:8px;width:100%;text-align:center">'+escape(text)+'</div>'
                    page.pdf(path=str(rendered), width=f'{section.page_width.inches}in',
                             height=f'{section.page_height.inches}in', print_background=True,
                             margin={name: f'{getattr(section,name+"_margin").inches}in'
                                     for name in ('top','bottom','left','right')},
                             display_header_footer=True, header_template=furniture(header)+header_images,
                             footer_template=furniture(footer)+
                             '<span style="font-size:8px" class="pageNumber"></span>')
                finally:
                    browser.close()
            with fitz.open(rendered) as pdf:
                if not pdf.page_count:
                    raise RuntimeError('Chrome produced an empty PDF.')
            destination = output_dir/rendered.name
            os.replace(rendered, destination)
            return destination, None
    except Exception as exc:
        return None, f'Chrome PDF export failed: {exc}'
