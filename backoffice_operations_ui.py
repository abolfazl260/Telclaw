"""Progressive enhancement for the Operations workspace; no business actions."""

OPERATIONS_CSS = """
.ops-workspace{display:grid;grid-template-columns:210px minmax(0,1fr);gap:1.4rem;align-items:start}
.ops-workspace>.ops-section{grid-column:2;margin:0 0 1rem}
.ops-sidebar{grid-column:1;grid-row:1 / span 12;position:sticky;top:1rem;padding:.65rem;background:#f3f6fb;border:1px solid #dce4ef;border-radius:12px}
.ops-sidebar h2{font-size:.75rem;text-transform:uppercase;letter-spacing:.08em;color:#64748b;margin:.65rem .55rem}
.ops-sidebar a{display:block;padding:.65rem .75rem;border-radius:8px;color:#334155;text-decoration:none;font-weight:600}
.ops-sidebar a:hover{background:#e6edf7}.ops-sidebar a[aria-current=location]{background:#174f9c;color:white}
.ops-overview{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:.75rem;margin:1rem 0}
.ops-metric{padding:1rem;border:1px solid #dce4ef;border-radius:12px;background:white}
.ops-metric strong{display:block;font-size:1.6rem;color:#174f9c}.ops-metric span{color:#536479;font-size:.85rem}
.ops-section[hidden]{display:none!important}.ops-section>h2{font-size:1.35rem}
.ops-grid{grid-template-columns:1fr!important}.op-card{min-width:0}
.op-card>h2{font-size:1.1rem}.op-actions{align-items:stretch}
.op-action{padding:.6rem!important;border:1px solid #e2e8f0!important;border-radius:9px!important;max-width:100%;box-sizing:border-box}
.op-action button{min-height:40px}.op-action button:disabled{cursor:not-allowed;opacity:.55}
.ops-recovery{width:100%;margin:.6rem 0;border:1px solid #dce4ef;border-radius:9px;padding:.7rem;box-sizing:border-box}
.ops-recovery summary,.op-result summary{cursor:pointer;font-weight:600;color:#475569}
.ops-result-metrics{display:flex;flex-wrap:wrap;gap:.45rem;margin:.6rem 0}
.ops-result-metrics span{background:#edf3fb;border-radius:6px;padding:.35rem .55rem;font-size:.85rem}
.op-state .muted{font-size:.8rem;overflow-wrap:anywhere}.ops-section pre{white-space:pre-wrap;overflow-wrap:anywhere;max-width:52ch}
.ops-section .scroll{max-height:480px;overflow:auto}.ops-section th{position:sticky;top:0;background:#f1f5fa;z-index:1}
.ops-section td{vertical-align:top}.ops-section td details{min-width:120px}.ops-section table{font-size:.85rem}
.ops-intro{display:flex;justify-content:space-between;gap:1rem;align-items:center}.ops-intro p{margin:.35rem 0}
.ops-running{padding:.7rem 1rem;background:#fff7dc;border-radius:9px;margin:.8rem 0}
.ops-disabled-help{font-size:.85rem;color:#64748b}.ops-sidebar a:focus-visible,summary:focus-visible{outline:3px solid #60a5fa;outline-offset:3px}
@media(max-width:760px){.ops-workspace{display:block}.ops-sidebar{position:static;display:flex;overflow-x:auto;gap:.3rem;margin-bottom:1rem}.ops-sidebar h2{display:none}.ops-sidebar a{white-space:nowrap}.ops-overview{grid-template-columns:repeat(2,minmax(0,1fr))}.ops-intro{display:block}.op-action{width:100%}.op-action label{width:100%}.op-action input,.op-action select{max-width:100%;box-sizing:border-box}.ops-form{grid-template-columns:minmax(0,1fr)!important}}
"""

OPERATIONS_JS = """
(()=>{
  const panels=[...document.querySelectorAll('.ops-workspace>.ops-section')];
  const links=[...document.querySelectorAll('.ops-sidebar a[href^="#"]')];
  if(!panels.length) return;
  const read=key=>{try{return sessionStorage.getItem(key)}catch(e){return null}};
  const write=(key,value)=>{try{sessionStorage.setItem(key,value)}catch(e){}};
  function select(id){
    if(!panels.some(panel=>panel.id===id)) id='pipeline';
    panels.forEach(panel=>panel.hidden=panel.id!==id);
    links.forEach(link=>{if(link.hash==='#'+id) link.setAttribute('aria-current','location');else link.removeAttribute('aria-current')});
    write('telclaw.ops.panel',id);
  }
  select(location.hash.slice(1)||read('telclaw.ops.panel')||'pipeline');
  window.addEventListener('hashchange',()=>select(location.hash.slice(1)));
  // Native links remain useful without JavaScript; modified clicks retain browser behavior.
  links.forEach(link=>link.addEventListener('click',event=>{
    if(event.ctrlKey||event.metaKey||event.shiftKey||event.altKey) return;
    select(link.hash.slice(1));
  }));
  document.querySelectorAll('.ops-workspace details').forEach((detail,index)=>{
    const key='telclaw.ops.detail.'+index;
    if(read(key)==='open') detail.open=true;
    detail.addEventListener('toggle',()=>write(key,detail.open?'open':'closed'));
  });
  // Do not discard partially entered forms while polling running operations.
  let editing=false;
  document.querySelectorAll('.ops-workspace form').forEach(form=>form.addEventListener('input',()=>editing=true));
  if(document.body.dataset.opsRunning==='true') setTimeout(()=>{
    if(!editing&&!document.querySelector('.ops-workspace input:focus,.ops-workspace select:focus')) location.reload();
  },10000);
})();
"""
