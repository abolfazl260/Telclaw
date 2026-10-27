"""Check deterministic rendering of real rows, independent of AI summaries."""
import asyncio
import json
import re
import shutil
import subprocess

import pytest
from backoffice_assistant import assistant_page


def test_query_rows_render_as_text_and_download_exact_data(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is needed for the UI script test')
    response = asyncio.run(assistant_page({'session': {'csrf': 'test'}, 'csp_nonce': 'test'}))
    script = re.search(r'<script[^>]*>(.*?)</script>', response.text, re.S).group(1)
    # Execute the actual renderer against a minimal DOM without a browser install.
    function = script[script.index('function appendQueryRows'):script.index('clear.onclick=')]
    text = '<img src=x onerror=alert(1)>\n' + 'Full source text ' * 200
    result = {'columns': ['local_id', 'raw_text', 'city', 'is_job_listing'],
              'rows': [[19637, text, None, 0]], 'row_count': 1,
              'truncated': False, 'truncated_cells': 0}
    program = '''
const assert=require('node:assert/strict');
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.textContent='';}
 append(...items){this.children.push(...items);}
 click(){} remove(){}
}
const document={createElement:tag=>new Element(tag),body:new Element('body')};
let downloaded;
class Blob {constructor(parts){downloaded=parts.join('');}}
const URL={createObjectURL:()=> 'blob:test',revokeObjectURL:()=>{}};
const setTimeout=()=>{};
''' + function + '\nconst data=' + json.dumps(result) + ''';
const article=new Element('article');
appendQueryRows(article,[{action:'query',data},{action:'rejected'}]);
const flat=node=>[node,...node.children.flatMap(flat)];
const nodes=flat(article), cells=nodes.filter(n=>n.tag==='td');
assert.equal(cells.length,4);
assert.equal(cells[0].textContent,'19637');
assert.equal(cells[1].textContent,data.rows[0][1]);
assert.equal(cells[2].textContent,'NULL');
assert.equal(cells[3].textContent,'0');
nodes.find(n=>n.tag==='button').onclick();
assert.deepEqual(JSON.parse(downloaded),data);
const limited=new Element('article');
appendQueryRows(limited,[{action:'query',data:{...data,truncated:true}}]);
assert(flat(limited).some(n=>n.textContent.includes('محدود شده')));
const empty=new Element('article');
appendQueryRows(empty,[{action:'query',data:{...data,rows:[]}}]);
assert(flat(empty).some(n=>n.textContent.includes('0 ردیف')));
'''
    path = tmp_path / 'render.cjs'
    path.write_text(program)
    checked = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=10)
    assert checked.returncode == 0, checked.stderr
    assert 'appendQueryRows(article,result.evidence);article.append(text)' in script
