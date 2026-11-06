"""Scoped layout and progressive enhancements for Message Manager."""
STYLE = '''<style>
.mm-tabs{display:flex;gap:.5rem;border-bottom:1px solid #ccd8e8;padding-bottom:.7rem;margin:1rem 0;flex-wrap:wrap}
.mm-tabs a,.mm-primary{padding:.6rem 1rem;border-radius:8px;text-decoration:none;background:white;color:#1957b8;border:1px solid #dce4ef}
.mm-tabs a[aria-current=page],.mm-primary{background:#1957b8;color:white}
.mm-editor-grid{display:grid;grid-template-columns:minmax(0,3fr) minmax(260px,2fr);gap:1.2rem;align-items:start}
.mm-editor-grid .backoffice-panel{margin:0;min-width:0}.mm-preview-panel{position:sticky;top:1rem}
#message-editor{display:block}fieldset{border:1px solid #dce4ef;border-radius:10px;padding:1rem;margin:0 0 1rem;min-width:0}legend{font-weight:650;padding:0 .4rem}
label{display:flex;flex-direction:column;gap:.3rem;margin:.5rem 0;min-width:0}input,select,textarea{max-width:100%;min-width:0}textarea{width:100%;resize:vertical}
form{display:flex;flex-wrap:wrap;gap:.7rem;align-items:end;margin:.7rem 0}.mm-actions,.mm-toolbar{display:flex;flex-wrap:wrap;gap:.6rem;align-items:center;margin:.7rem 0}
.mm-toolbar button{background:#edf3fc;color:#1957b8;border-color:#ccd8e8}.mm-button-item{align-items:end;display:grid;grid-template-columns:1fr 2fr 80px auto;gap:.5rem;border-bottom:1px solid #dce4ef;padding:.5rem 0}
.mm-empty{text-align:center;padding:2rem;color:#536479}.mm-table{overflow-x:auto}table{border-collapse:collapse;width:100%;margin:1rem 0}th,td{padding:.7rem;text-align:left;border-bottom:1px solid #dce4ef;vertical-align:top;overflow-wrap:anywhere}th{background:#f6f8fc}td form{margin:0}
.mm-badge{display:inline-block;padding:.2rem .6rem;border-radius:20px;font-size:.85rem}.good{background:#e6f5ed;color:#17633f}.bad{background:#ffebe8;color:#963525}.neutral{background:#edf0f5;color:#536479}
.telegram-preview{background:#eef7ed;padding:1rem;border-radius:12px;overflow-wrap:anywhere;min-height:100px}.telegram-preview img,.telegram-preview video{max-width:100%;max-height:320px}.keyboard{display:flex;gap:.4rem;margin-top:.5rem}.keyboard a{background:white;padding:.4rem;border-radius:6px;flex:1;text-align:center;overflow-wrap:anywhere}
label:has(input[type=checkbox]){flex-direction:row;align-items:center;width:100%}.mm-publish>form{align-items:start}.mm-schedule form{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));align-items:end}.mm-schedule label:has(select[multiple]),.mm-schedule label:has(input[type=checkbox]){grid-column:1/-1}
pre,code{white-space:pre-wrap;overflow-wrap:anywhere}details{margin:1rem 0}summary{cursor:pointer;font-weight:600}tg-spoiler{background:#ccc}
@media(max-width:900px){.mm-editor-grid{grid-template-columns:1fr}.mm-preview-panel{position:static}.mm-button-item{grid-template-columns:1fr 1fr}.mm-tabs a{flex:1 1 40%;text-align:center}.mm-schedule form{grid-template-columns:1fr}}
</style>'''

SCRIPT = r'''(()=>{
const editor=document.getElementById('message-editor');if(!editor)return;
const text=editor.elements.text, mode=editor.elements.parse_mode, raw=editor.elements.buttons;
const builder=document.getElementById('button-builder'), asset=document.getElementById('asset-key');
const status=document.getElementById('preview-status'), output=document.getElementById('live-preview');
let timer,sequence=0,dirty=editor.dataset.unsaved==='true';
if(dirty)document.getElementById('save-status').textContent='Unsaved preview — save before publishing.';
function changed(){dirty=true;document.getElementById('save-status').textContent='Unsaved changes — save before publishing.';clearTimeout(timer);timer=setTimeout(updatePreview,450)}
async function updatePreview(){const current=++sequence;if(!text.value.trim()){output.textContent='Your message preview will appear here.';status.textContent='';return}
const data=new FormData(editor);data.set('fragment','yes');try{const response=await fetch('/message-manager/preview',{method:'POST',body:data});if(!response.ok)throw new Error(await response.text());const result=await response.json();if(current!==sequence)return;output.innerHTML=result.html;status.textContent=''}catch(error){if(current===sequence)status.textContent=error.message}}
function serializeButtons(){const groups=new Map();builder.querySelectorAll('.mm-button-item').forEach(item=>{const row=Number(item.querySelector('[data-field=row]').value);if(!groups.has(row))groups.set(row,[]);groups.get(row).push({text:item.querySelector('[data-field=text]').value,url:item.querySelector('[data-field=url]').value})});raw.value=JSON.stringify([...groups].sort((a,b)=>a[0]-b[0]).map(pair=>pair[1]));changed()}
function addButton(button={text:'',url:''},row=1){const item=document.createElement('div');item.className='mm-button-item';for(const [field,label,value,type] of [['text','Button text',button.text,'text'],['url','Button URL',button.url,'text'],['row','Row',row,'number']]){const wrapper=document.createElement('label');wrapper.textContent=label;const input=document.createElement('input');input.type=type;input.dataset.field=field;input.value=value;input.setAttribute('aria-label',label);if(field==='row'){input.min=1;input.max=20}input.addEventListener('input',serializeButtons);wrapper.append(input);item.append(wrapper)}const remove=document.createElement('button');remove.type='button';remove.textContent='Remove';remove.addEventListener('click',()=>{item.remove();serializeButtons()});item.append(remove);builder.append(item)}
function loadButtons(){try{const rows=JSON.parse(raw.value);if(!Array.isArray(rows)||rows.some(row=>!Array.isArray(row)))throw new Error('Invalid keyboard');builder.replaceChildren();rows.forEach((row,i)=>row.forEach(button=>addButton(button,i+1)))}catch(error){status.textContent='Check advanced keyboard JSON before saving.'}}
loadButtons();document.getElementById('add-button').addEventListener('click',()=>{addButton();serializeButtons()});raw.addEventListener('change',()=>{loadButtons();changed()});
document.querySelectorAll('.mm-publish form').forEach(form=>{if(['/message-manager/publish','/message-manager/schedule'].includes(form.getAttribute('action')))form.addEventListener('submit',event=>{if(dirty){event.preventDefault();document.getElementById('save-status').textContent='Save your changes before publishing.';editor.scrollIntoView({behavior:'smooth'});}})});
editor.addEventListener('input',changed);editor.addEventListener('change',changed);
const escapeHTML=value=>value.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
document.querySelectorAll('[data-format]').forEach(button=>{button.addEventListener('click',()=>{let start=text.selectionStart,end=text.selectionEnd;if(mode.value!=='HTML'){const original=text.value;start=escapeHTML(original.slice(0,start)).length;end=escapeHTML(original.slice(0,end)).length;text.value=escapeHTML(original);mode.value='HTML'}const tag=button.dataset.format;let open='<'+tag+'>';if(tag==='a'){const url=prompt('Link URL (https:// or tg://)');if(!url)return;open='<a href="'+escapeHTML(url)+'">'}const selected=text.value.slice(start,end)||'text';text.setRangeText(open+selected+'</'+tag+'>',start,end,'select');text.focus();changed()})});
const upload=document.getElementById('upload'), fileInput=document.getElementById('media-file'), uploadResult=document.getElementById('upload-result');
upload.addEventListener('click',async()=>{const file=fileInput.files[0];if(!file){uploadResult.textContent='Select a file first';return}upload.disabled=true;uploadResult.textContent='Uploading…';try{const response=await fetch('/message-manager/media',{method:'POST',headers:{'X-CSRF-Token':__CSRF__,'X-File-Name':encodeURIComponent(file.name),'Content-Type':file.type||'application/octet-stream'},body:file});if(!response.ok)throw new Error(await response.text());const result=await response.json();asset.value=result.asset_key;uploadResult.textContent=result.name+' · attached (save the draft)';changed()}catch(error){uploadResult.textContent=error.message}finally{upload.disabled=false}});
document.getElementById('remove-media').addEventListener('click',()=>{asset.value='';fileInput.value='';uploadResult.textContent='No file attached';changed()});
})();'''
