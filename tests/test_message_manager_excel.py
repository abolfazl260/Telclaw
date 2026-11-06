import io
from zipfile import ZipFile, ZIP_DEFLATED
import pytest
from openpyxl import load_workbook
from message_manager.excel_service import template, workbook_bytes
from message_manager.validation import ValidationError


def workbook(rows=None,button_rows=None):
    book = load_workbook(io.BytesIO(template()))
    for row in rows or [['campaign-1','@firstchannel;@secondchannel','<b>Hello</b>','2099-01-01','10:00','daily']]:
        book['Messages'].append(row)
    for row in button_rows or []:
        book['Buttons'].append(row)
    return workbook_bytes(book)


def test_template_preview_confirm_and_idempotency(mm):
    raw = workbook(button_rows=[['campaign-1','Open','https://t.me/example',1,1],['campaign-1','User','tg://user?id=42',2,1]])
    bid, preview = mm.excel.preview(raw,'campaign',42)
    assert preview['errors'] == 0
    assert len(preview['rows'][0]['content']['buttons']) == 2
    assert not mm.repo.list_messages()
    with pytest.raises(ValidationError,match='Confirm'):
        mm.excel.confirm(bid,42)
    mm.excel.confirm(bid,42,True)
    mm.excel.confirm(bid,42,True)
    assert len(mm.repo.list_messages()) == 1
    with mm.repo.connection() as c:
        assert c.execute('SELECT COUNT(*) FROM mm_schedules').fetchone()[0] == 0
    _, duplicate = mm.excel.preview(raw,'campaign',42)
    assert duplicate['errors'] == 1 and 'already imported' in duplicate['rows'][0]['errors'][0]


@pytest.mark.asyncio
async def test_import_activation_requires_separate_confirmation(mm):
    bid,_ = mm.excel.preview(workbook(),'campaign',42)
    mm.excel.confirm(bid,42,True)
    with pytest.raises(ValidationError,match='Confirm activation'):
        await mm.excel.activate(bid,42)
    await mm.excel.activate(bid,42,True)
    with mm.repo.connection() as c:
        assert c.execute('SELECT COUNT(*) FROM mm_schedules').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM mm_schedule_targets').fetchone()[0] == 2
    with pytest.raises(ValidationError):
        await mm.excel.activate(bid,42,True)


@pytest.mark.parametrize('rows,button_rows,expected',[
    ([['a','@unknown','Body','2099-01-01','10:00','once']],[],'Unknown'),
    ([['a','@firstchannel','','2099-01-01','10:00','once']],[],'Message must'),
    ([['a','@firstchannel','Body','bad','10:00','once']],[],'Invalid isoformat'),
    ([['a','@firstchannel','Body','2099-01-01','10:00','nonsense']],[],'Invalid recurrence'),
    ([['a','@firstchannel','=1+1','2099-01-01','10:00','once']],[],'Formulas'),
    ([['a','@firstchannel','Body','2099-01-01','10:00','once'],['a','@firstchannel','Body','2099-01-01','10:00','once']],[],'Duplicate'),
    (None,[['unknown','Open','https://example.com',1,1]],'no matching'),
    (None,[['campaign-1','Open','javascript:bad',1,1]],'HTTP(S)'),
])
def test_row_level_errors_prevent_committing(mm,rows,button_rows,expected):
    bid,preview = mm.excel.preview(workbook(rows,button_rows),'campaign',42)
    assert preview['errors']
    assert expected in str(preview)
    with pytest.raises(ValidationError,match='Correct all'):
        mm.excel.confirm(bid,42,True)
    assert not mm.repo.list_messages()


def test_duplicate_import_is_atomic_if_namespace_changed_after_preview(mm):
    raw = workbook()
    one,_ = mm.excel.preview(raw,'campaign',42)
    two,_ = mm.excel.preview(raw,'campaign',42)
    mm.excel.confirm(one,42,True)
    with pytest.raises(ValidationError,match='Duplicate import'):
        mm.excel.confirm(two,42,True)
    assert len(mm.repo.list_messages()) == 1


@pytest.mark.asyncio
async def test_changed_draft_not_activated_from_stale_preview(mm):
    bid,_ = mm.excel.preview(workbook(),'campaign',42)
    mm.excel.confirm(bid,42,True)
    message = mm.repo.list_messages()[0]
    mm.service.save({'id':message['id'],'version':1,'title':'Edited','text':'Edited'},42)
    with pytest.raises(ValidationError,match='has changed'):
        await mm.excel.activate(bid,42,True)
    with mm.repo.connection() as c:
        assert c.execute('SELECT COUNT(*) FROM mm_schedules').fetchone()[0] == 0


def test_xlsx_bomb_size_rows_sheets_and_headers(mm,monkeypatch):
    import config
    with pytest.raises(ValidationError):
        mm.excel.preview(b'not a workbook','test',42)
    book = load_workbook(io.BytesIO(workbook()))
    book['Messages']['A1'] = 'missing'
    with pytest.raises(ValidationError,match='headers'):
        mm.excel.preview(workbook_bytes(book),'test',42)
    book = load_workbook(io.BytesIO(workbook()))
    book.create_sheet('Unexpected')
    with pytest.raises(ValidationError,match='exactly'):
        mm.excel.preview(workbook_bytes(book),'test',42)
    monkeypatch.setattr(config,'MESSAGE_MANAGER_XLSX_MAX_ROWS',1)
    with pytest.raises(ValidationError,match='row limit'):
        mm.excel.preview(workbook([['a','@firstchannel','One','2099-01-01','10:00','once'],['b','@firstchannel','Two','2099-01-01','10:00','once']]),'test',42)
    bomb = io.BytesIO()
    with ZipFile(bomb,'w',ZIP_DEFLATED) as z:
        z.writestr('large',b'x'*(33*1024*1024))
    with pytest.raises(ValidationError,match='safe limit'):
        mm.excel.preview(bomb.getvalue(),'test',42)


def test_export_never_emits_formula_cells(mm):
    from test_message_manager_repository import scheduled, NOW
    mid,sid = scheduled(mm,(1,))
    with mm.repo.connection(True) as c:
        c.execute("UPDATE mm_messages SET title='  =HYPERLINK(\"bad\")' WHERE id=?",(mid,))
    mm.repo.materialize(NOW)
    book = load_workbook(io.BytesIO(mm.excel.export()),data_only=False)
    assert all(cell.data_type != 'f' for row in book.active for cell in row)
    assert book.active['B2'].value.startswith("'")
    assert book.active['E2'].value.startswith("'@")


def test_optional_button_positions_append_in_order(mm):
    _,preview = mm.excel.preview(workbook(button_rows=[['campaign-1','One','https://example.com'],['campaign-1','Two','https://example.org']]),'buttons',42)
    assert preview['errors'] == 0
    assert [b['text'] for b in preview['rows'][0]['content']['buttons'][0]] == ['One','Two']


@pytest.mark.asyncio
async def test_many_imported_messages_check_each_destination_once(mm):
    rows = [[f'key-{i}','@firstchannel;@secondchannel','Text','2099-01-01','10:00','once'] for i in range(10)]
    bid,preview = mm.excel.preview(workbook(rows),'batch-check',42)
    mm.excel.confirm(bid,42,True)
    await mm.excel.activate(bid,42,True)
    assert sorted(mm.sender.checked) == [1,2]
