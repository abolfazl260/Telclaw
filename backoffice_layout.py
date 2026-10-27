"""Shared visual frame and navigation for every authenticated back-office page."""
from html import escape

SECTIONS = (
    ('publishing', '/', 'Publishing'),
    ('operations', '/operations', 'Operations'),
    ('cargo', '/cargo', 'Active Cargo'),
    ('assistant', '/assistant', 'AI Assistant'),
    ('data', '/data', 'Database'),
    ('import', '/data/import', 'Import CSV'),
    ('normalization', '/normalization', 'Normalization'),
    ('health', '/health', 'System Health'),
    ('settings', '/settings', 'Settings'),
)

SHARED_STYLE = '''<style id="backoffice-shared-style">
*{box-sizing:border-box}
body{font:15px/1.5 system-ui,sans-serif;background:#f3f6fb;color:#192333;
max-width:1500px;margin:0 auto;padding:1.5rem;direction:ltr}
.backoffice-header{direction:ltr;text-align:left}
.backoffice-header h1{font-size:2rem;line-height:1.25;margin:.2rem 0;font-weight:700}
.backoffice-header .tabs{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center;margin:1.3rem 0 1rem;padding:0}
.backoffice-header .tabs a{display:inline-block;padding:.5rem .8rem;background:white;color:#1957b8;
text-decoration:none;border-radius:8px;border:1px solid #d9e2ee;font-weight:400;white-space:nowrap}
.backoffice-header .tabs a.active{background:#1957b8;color:white;border-color:#1957b8}
.backoffice-header .tabs a:hover{background:#eaf0fa;color:#164c9b}
.backoffice-header .tabs a.active:hover{background:#174f9c;color:white}
body button{font:inherit;padding:.5rem .8rem;border:1px solid #1957b8;border-radius:7px;
background:#1957b8;color:white;cursor:pointer;line-height:1.5}
body button:disabled{opacity:.55;cursor:not-allowed}
body input,body select,body textarea{font:inherit;max-width:100%}
body input:not([type=checkbox]):not([type=radio]):not([type=hidden]),body select,body textarea{
border:1px solid #c8d3e2;border-radius:7px;padding:.5rem .65rem;background:white;color:#192333}
body input[type=file]::file-selector-button{font:inherit;border:0;border-radius:5px;padding:.3rem .65rem;
background:#eaf0fa;color:#1957b8;cursor:pointer;margin-inline-end:.65rem}
body>section,.backoffice-panel{padding:1rem;background:white;border:1px solid #dce4ef;border-radius:14px}
.backoffice-panel{margin:1rem 0;min-width:0}
.backoffice-panel h2{margin:.1rem 0 .7rem;font-size:1.35rem}
.backoffice-panel p{overflow-wrap:anywhere}
.backoffice-muted{color:#536479}
.backoffice-notice{border:1px solid #cdddf3;background:#f5f8ff;padding:.75rem 1rem;border-radius:9px}
.backoffice-error{background:#fff0ed;color:#8d281b;border:1px solid #f2c4bc;border-radius:9px;padding:.75rem 1rem}
.backoffice-content{min-width:0}
a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{
outline:3px solid #90b9f4;outline-offset:2px}
@media(max-width:640px){body{padding:1rem}.backoffice-header h1{font-size:1.6rem}
.backoffice-header .tabs{gap:.4rem}.backoffice-header .tabs a{padding:.45rem .6rem}}
</style>'''


def page_header(active, title):
    links = []
    for key, url, label in SECTIONS:
        current = ' class="active" aria-current="page"' if key == active else ''
        links.append(f'<a href="{url}"{current}>{label}</a>')
    return (f'<header class="backoffice-header" dir="ltr"><h1>{escape(title)}</h1>'
            '<nav class="tabs" aria-label="Back office sections">' + ''.join(links) + '</nav></header>')
