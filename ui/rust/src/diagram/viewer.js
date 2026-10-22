'use strict';
(() => {
  const data = JSON.parse(document.getElementById('schema-data').textContent);
  const sourceHTML = '<!doctype html>\n' + document.documentElement.outerHTML;
  const $ = id => document.getElementById(id);
  const svg = $('graph'), ns = 'http://www.w3.org/2000/svg';
  const tables = new Map(data.tables.map(t => [t.id,t]));
  const positions = new Map(), sizes = new Map(), ports = new Map();
  const links = data.foreign_keys;
  const edgeById = new Map(links.map(f=>[f.id,f]));
  const adjacency = new Map(data.tables.map(t=>[t.id,[]]));
  for(const f of links){adjacency.get(f.source).push(f);if(f.target!==f.source)adjacency.get(f.target).push(f);}
  const palette = ['#7dd3c7','#96baff','#c0a1f2','#f4bf75','#e99bb6','#9ecb80'];
  const schemas = [...new Set(data.tables.map(t => t.schema))].sort();
  const colors = new Map(schemas.map((s,i) => [s,palette[i%palette.length]]));
  let selected = data.focus || null, anchor = selected, selectedEdge = null, visible = [];
  let camera = {x:0,y:0,w:1200,h:800}, bounds = {x:0,y:0,w:1200,h:800};
  let drag = null, noticeTimer;
  const make = (tag, attrs={}, text=null, parent=null) => {
    const el = document.createElementNS(ns, tag);
    for (const [k,v] of Object.entries(attrs)) el.setAttribute(k,String(v));
    if (text !== null) el.textContent = text;
    if (parent) parent.append(el);
    return el;
  };
  const html = (tag, text, parent, className) => {
    const el = document.createElement(tag);
    if (text !== null) el.textContent = text;
    if (className) el.className = className;
    if (parent) parent.append(el);
    return el;
  };
  const fullName = t => t.external ? t.reference : `${t.schema}.${t.name}`;
  const short = (s,n) => { const c = Array.from(s); return c.length>n?c.slice(0,n-1).join('')+'…':s; };
  const incident = id => adjacency.get(id) || [];
  const neighbourIds = id => new Set([id,...incident(id).flatMap(f => [f.source,f.target])]);
  const flags = (table,column) => {
    const keys = table.keys.filter(k => k.columns.includes(column.name));
    return [keys.some(k=>k.primary)?'PK':'',incident(table.id).some(f=>f.source===table.id&&f.pairs.some(p=>p[0]===column.name))?'FK':'',keys.some(k=>!k.primary)?'UK':''].filter(Boolean);
  };
  const shownColumns = t => !$('keys-only').checked ? t.columns : t.columns.filter(c => flags(t,c).length || incident(t.id).some(f=>f.target===t.id&&f.pairs.some(p=>p[1]===c.name)));
  function scope() {
    if ($('mode').value==='neighbours' && anchor) {
      const ids = neighbourIds(anchor); return data.tables.filter(t=>ids.has(t.id));
    }
    return data.tables.filter(t=>!$('schema').value||t.schema===$('schema').value);
  }
  function isMatch(t) {
    const q = $('search').value.trim().toLocaleLowerCase();
    return !q || [fullName(t),...t.columns.map(c=>c.name)].some(s=>s.toLocaleLowerCase().includes(q));
  }
  function buildList() {
    const list = $('table-list'); list.replaceChildren();
    let previous = null, count = 0;
    for (const t of scope().filter(isMatch)) {
      if (previous!==t.schema) { html('div',t.schema,list,'schema-heading'); previous=t.schema; }
      const b = html('button',null,list,'table-button'+(selected===t.id?' active':''));
      html('span',t.name,b,'label'); html('span',String(t.columns.length),b,'count');
      b.title=fullName(t); b.onclick=()=>selectTable(t.id); count++;
    }
    if (!count) html('p','No matching tables.',list,'intro');
  }
  // Strongly connected tables share a rank, so cycles never make layout diverge.
  function ranks(nodes) {
    const ids = new Set(nodes.map(t=>t.id)), adj = new Map(nodes.map(t=>[t.id,[]]));
    for (const f of links) if (ids.has(f.source)&&ids.has(f.target)&&f.source!==f.target) adj.get(f.target).push(f.source);
    let seq=0, index=new Map(), low=new Map(), stack=[], on=new Set(), components=[];
    function visit(id) {
      index.set(id,seq); low.set(id,seq++); stack.push(id); on.add(id);
      for (const child of adj.get(id)) {
        if (!index.has(child)) { visit(child); low.set(id,Math.min(low.get(id),low.get(child))); }
        else if (on.has(child)) low.set(id,Math.min(low.get(id),index.get(child)));
      }
      if (low.get(id)===index.get(id)) {
        const component=[]; let v;
        do { v=stack.pop(); on.delete(v); component.push(v); } while(v!==id);
        components.push(component);
      }
    }
    for (const t of nodes) if (!index.has(t.id)) visit(t.id);
    const owner=new Map(); components.forEach((c,i)=>c.forEach(id=>owner.set(id,i)));
    const edges=components.map(()=>new Set()), degree=components.map(()=>0), rank=components.map(()=>0);
    for (const [id, children] of adj) for(const child of children) {
      const a=owner.get(id),b=owner.get(child);
      if(a!==b&&!edges[a].has(b)){edges[a].add(b);degree[b]++;}
    }
    const queue=degree.flatMap((d,i)=>d===0?[i]:[]);
    for(let i=0;i<queue.length;i++)for(const next of edges[queue[i]]){
      rank[next]=Math.max(rank[next],Math.min(5,rank[queue[i]]+1));
      if(--degree[next]===0)queue.push(next);
    }
    return new Map(nodes.map(t=>[t.id,rank[owner.get(t.id)]]));
  }
  function measure() {
    sizes.clear(); ports.clear();
    for(const t of visible){
      const cols=shownColumns(t), width=Math.max(350,Math.min(540,Math.max(t.name.length*8+85,...cols.map(c=>c.name.length*7+c.data_type.length*6+115))));
      sizes.set(t.id,{w:width,h:64+Math.max(1,cols.length)*24+12,cols});
      ports.set(t.id,new Map(cols.map((c,i)=>[c.name,76+i*24])));
    }
  }
  function autoLayout() {
    const rank=ranks(visible), groups=[...new Set(visible.map(t=>t.schema))];
    let offsetY=50;
    for(const schema of groups){
      const group=visible.filter(t=>t.schema===schema), levels=[...new Set(group.map(t=>rank.get(t.id)))].sort((a,b)=>a-b);
      let offsetX=60, maxY=offsetY;
      for(const level of levels){
        let y=offsetY+35, width=0;
        for(const t of group.filter(t=>rank.get(t.id)===level)){
          positions.set(t.id,{x:offsetX,y}); const size=sizes.get(t.id);
          y+=size.h+55;width=Math.max(width,size.w);
        }
        maxY=Math.max(maxY,y);offsetX+=width+150;
      }
      offsetY=maxY+70;
    }
  }
  function calculateBounds() {
    if(!visible.length){bounds={x:0,y:0,w:1200,h:800};return;}
    // Include curved self-links and long schema labels as well as table boxes.
    try { const box=svg.getBBox(); if(box.width&&box.height){bounds={x:box.x-40,y:box.y-40,w:box.width+80,h:box.height+80};return;} } catch (_) { /* Geometry fallback before first paint. */ }
    const rects=visible.map(t=>({...positions.get(t.id),...sizes.get(t.id)}));
    const x=Math.min(...rects.map(r=>r.x))-55,y=Math.min(...rects.map(r=>r.y))-55;
    bounds={x,y,w:Math.max(...rects.map(r=>r.x+r.w))-x+110,h:Math.max(...rects.map(r=>r.y+r.h))-y+75};
  }
  function applyCamera(){
    svg.setAttribute('viewBox',`${camera.x} ${camera.y} ${camera.w} ${camera.h}`);
    $('zoom-label').textContent=Math.round(svg.clientWidth/camera.w*100)+'%';
  }
  function fit(){
    calculateBounds(); const aspect=Math.max(1,svg.clientWidth)/Math.max(1,svg.clientHeight);
    camera={...bounds}; if(camera.w/camera.h>aspect){const h=camera.w/aspect;camera.y-=(h-camera.h)/2;camera.h=h;}else{const w=camera.h*aspect;camera.x-=(w-camera.w)/2;camera.w=w;}
    applyCamera();
  }
  function zoom(factor,x=0.5,y=0.5){
    const w=Math.max(180,Math.min(100000,camera.w*factor)),ratio=w/camera.w,h=camera.h*ratio;
    camera.x+=(camera.w-w)*x;camera.y+=(camera.h-h)*y;camera.w=w;camera.h=h;applyCamera();
  }
  function centerTable(id){
    const p=positions.get(id),size=sizes.get(id);if(!p||!size)return;
    const aspect=Math.max(1,svg.clientWidth)/Math.max(1,svg.clientHeight),w=Math.max(size.w*2,700),h=Math.max(size.h*1.4,w/aspect);
    camera={x:p.x+size.w/2-h*aspect/2,y:p.y+size.h/2-h/2,w:h*aspect,h};applyCamera();
  }
  function drawClusters(){
    $('clusters').replaceChildren();
    for(const schema of [...new Set(visible.map(t=>t.schema))]){
      const nodes=visible.filter(t=>t.schema===schema),left=Math.min(...nodes.map(t=>positions.get(t.id).x))-22,top=Math.min(...nodes.map(t=>positions.get(t.id).y))-40;
      const right=Math.max(...nodes.map(t=>positions.get(t.id).x+sizes.get(t.id).w))+22,bottom=Math.max(...nodes.map(t=>positions.get(t.id).y+sizes.get(t.id).h))+22;
      make('rect',{x:left,y:top,width:right-left,height:bottom-top,rx:14,class:'cluster'},null,$('clusters'));
      make('text',{x:left+14,y:top+24,class:'cluster-label',style:`fill:${colors.get(schema)}`},schema,$('clusters'));
    }
  }
  function drawEdges(){
    $('edges').replaceChildren();const ids=new Set(visible.map(t=>t.id));
    const siblings=new Map();
    for(const f of links){
      if(!ids.has(f.source)||!ids.has(f.target))continue;
      const key=[f.source,f.target].sort().join('/'),slot=siblings.get(key)||0;siblings.set(key,slot+1);
      const g=make('g',{'data-edge':f.id,class:'edge',tabindex:0,role:'button','aria-label':f.name},null,$('edges'));
      make('title',{},`${f.name}: ${fullName(tables.get(f.source))} → ${fullName(tables.get(f.target))}\n${f.pairs.map(p=>p.join(' → ')).join('\n')}`,g);
      const a=positions.get(f.source),b=positions.get(f.target),sa=sizes.get(f.source),sb=sizes.get(f.target);
      f.pairs.forEach(([src,dst],i)=>{
        const ay=a.y+(ports.get(f.source).get(src)||40),by=b.y+(ports.get(f.target).get(dst)||40);
        let ax,bx,d,endDirection;
        if(f.source===f.target){
          ax=a.x+sa.w;bx=ax;const loop=ax+65+slot*24+i*12;
          d=`M${ax},${ay} C${loop},${ay} ${loop},${by-38} ${bx},${by}`;endDirection=-1;
        }else{
          const right=b.x+sb.w/2>=a.x+sa.w/2;
          ax=right?a.x+sa.w:a.x;bx=right?b.x:b.x+sb.w;
          const bend=Math.max(55,Math.abs(bx-ax)*0.5)+slot*20+i*9;
          d=`M${ax},${ay} C${ax+(right?bend:-bend)},${ay} ${bx+(right?-bend:bend)},${by} ${bx},${by}`;endDirection=right?1:-1;
        }
        make('path',{d},null,g);
        // Arrow points to the referenced parent column.
        make('path',{d:`M${bx-endDirection*8},${by-4} L${bx},${by} L${bx-endDirection*8},${by+4} Z`,class:'arrow',style:'stroke:none'},null,g);
        if(i===0){
          const nearSource=ax+(ax===a.x?-10:10), nearTarget=bx+(endDirection===1?-12:12);
          make('text',{x:nearSource,y:ay-9,'text-anchor':nearSource<ax?'end':'start',class:'cardinality'},f.unique?'0..1':'0..*',g);
          make('text',{x:nearTarget,y:by+17,'text-anchor':nearTarget<bx?'end':'start',class:'cardinality'},f.optional?'0..1':'1',g);
          const labelX=f.source===f.target?ax+35:(ax+bx)/2,labelY=(ay+by)/2-7+slot*15;
          make('text',{x:labelX,y:labelY,'text-anchor':'middle',class:'edge-label'},short(f.name,32),g);
        }
      });
      g.onclick=e=>{e.stopPropagation();selectedEdge=f.id;selected=null;highlight();details();buildList();document.body.classList.add('show-details');};
      g.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();g.onclick(e);}};
    }
  }
  function drawTables(){
    $('tables').replaceChildren();
    for(const t of visible){
      const p=positions.get(t.id),size=sizes.get(t.id),g=make('g',{'data-table':t.id,transform:`translate(${p.x} ${p.y})`,class:'table',tabindex:0,role:'button','aria-label':fullName(t)},null,$('tables'));
      make('rect',{width:size.w,height:size.h,rx:10,class:'card'},null,g);
      make('rect',{width:size.w-2,height:55,x:1,y:1,rx:9,class:'heading'},null,g);
      make('rect',{x:0,y:10,width:3,height:35,rx:1,style:`fill:${colors.get(t.schema)}`},null,g);
      make('text',{x:13,y:23,class:'name'},short(t.name,Math.floor((size.w-25)/8)),g);
      make('text',{x:13,y:43,class:'kind'},`${t.kind} · ${t.columns.length} columns`,g);
      make('title',{},`${fullName(t)}\n${t.reference}`,g);
      if(!size.cols.length)make('text',{x:13,y:77,class:'kind'},t.columns.length?'No declared keys':'No column metadata',g);
      size.cols.forEach((c,i)=>{
        const y=76+i*24,row=make('g',{},null,g),marks=flags(t,c);
        make('line',{x1:10,x2:size.w-10,y1:y+9,y2:y+9,class:'row-line'},null,row);
        make('text',{x:12,y,class:'key'},marks.join(' '),row);
        const split=Math.round(size.w*.61);
        make('text',{x:marks.length?73:13,y,class:'column-name'},short(c.name,Math.max(8,Math.floor((split-(marks.length?80:20))/7))),row);
        make('text',{x:split,y,class:'column-type'},short(c.data_type||'ANY',Math.max(6,Math.floor((size.w-split-48)/6))),row);
        make('text',{x:size.w-12,y,'text-anchor':'end',class:'nullable'},c.nullable?'NULL':'',row);
        make('title',{},`${c.name}: ${c.data_type||'untyped'} · ${c.nullable?'nullable':'NOT NULL'}${c.default===null?'':`\nDefault: ${c.default}`}${c.generated?'\nGenerated column':''}${c.identity?'\nIdentity column':''}`,row);
      });
      g.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();e.stopPropagation();selectTable(t.id,false);}};
    }
  }
  function highlight(){
    const related=selected?neighbourIds(selected):selectedEdge?new Set([edgeById.get(selectedEdge).source,edgeById.get(selectedEdge).target]):null;
    for(const el of $('tables').children){const id=el.getAttribute('data-table');el.classList.toggle('selected',id===selected);el.classList.toggle('neighbour',related?.has(id)&&id!==selected);el.classList.toggle('dim',!!related&&!related.has(id));el.classList.toggle('match',!!$('search').value&&isMatch(tables.get(id)));}
    for(const el of $('edges').children){const f=edgeById.get(el.getAttribute('data-edge')),active=selectedEdge===f.id||selected&&(f.source===selected||f.target===selected);el.classList.toggle('selected',!!active);el.classList.toggle('dim',!!related&&!active);}
  }
  function details(){
    const panel=$('details');panel.replaceChildren();
    if(selectedEdge){
      const f=edgeById.get(selectedEdge);
      html('h2',f.name,panel);html('p','FOREIGN KEY',panel,'meta');
      for(const [id,label] of [[f.source,'Child table'],[f.target,'Parent table']]){html('h3',label,panel);const b=html('button',fullName(tables.get(id)),panel,'relation-button');b.onclick=()=>selectTable(id);}
      html('h3','Column mappings',panel);const mapping=html('div',null,panel,'mapping');for(const pair of f.pairs)html('div',pair.join(' → '),mapping);
      html('h3','Constraint actions',panel);html('p',`ON UPDATE ${f.on_update}\nON DELETE ${f.on_delete}\nMATCH ${f.match_type}`,panel).style.whiteSpace='pre-line';
      html('p',`Each child → ${f.optional?'zero or one':'one'} parent. Each parent → ${f.unique?'zero or one child':'zero or more children'}.`,panel);
      if(!f.validated)html('p','NOT VALID: existing rows may violate this constraint.',panel,'warning');
    }else if(selected){
      const t=tables.get(selected);html('h2',t.name,panel);html('p',`${t.schema} · ${t.kind}`,panel,'meta');
      html('span',`${incident(t.id).length} relationships`,panel,'pill');
      html('h3','Keys',panel);if(!t.keys.length)html('p','No primary or unique keys declared.',panel,'intro');
      for(const k of t.keys)html('p',`${k.primary?'PK':'UNIQUE'} (${k.columns.join(', ')}) · ${k.name}`,panel);
      for(const [label,outgoing] of [['References',true],['Referenced by',false]]){
        const related=links.filter(f=>outgoing?f.source===t.id:f.target===t.id);html('h3',`${label} · ${related.length}`,panel);
        for(const f of related){const b=html('button',`${fullName(tables.get(outgoing?f.target:f.source))} · ${f.name}`,panel,'relation-button');b.onclick=()=>{selected=null;selectedEdge=f.id;highlight();details();buildList();};}
      }
      html('h3',`Columns · ${t.columns.length}`,panel);
      for(const c of t.columns){const row=html('div',null,panel,'column-detail');html('strong',c.name,row);html('div',`${c.data_type||'untyped'} · ${c.nullable?'nullable':'NOT NULL'}${c.generated?' · generated':''}${c.identity?' · identity':''}`,row,'meta');if(c.default!==null)html('code',`Default: ${c.default}`,row);}
    }else{
      html('h2','Follow the relationships',panel);html('p','Choose a table to highlight its parents and children. Choose a connection to inspect its exact foreign key mapping.',panel,'intro');
      html('h3','Reading the diagram',panel);html('p','Arrows point from referencing (child) columns to referenced (parent) columns. PK = primary key, FK = foreign key, UK = member of a unique key.',panel);
      html('p','Composite key columns belong to one key; a UK badge does not imply that the column is individually unique.',panel,'intro');
    }
    if(data.warnings.length){html('h3','Metadata warnings',panel);for(const warning of data.warnings)html('p',warning,panel,'warning');}
    html('p','Snapshot of declared schema only. Relationships in application code or JSON are not inferred. Reopen D in dbcli to refresh.',panel,'meta');
  }
  function render(layout=false,fitView=false){
    visible=scope();measure();if(layout||visible.some(t=>!positions.has(t.id)))autoLayout();
    drawClusters();drawEdges();drawTables();highlight();buildList();details();calculateBounds();
    $('empty').hidden=visible.length!==0;
    const ids=new Set(visible.map(t=>t.id)),count=links.filter(f=>ids.has(f.source)&&ids.has(f.target)).length;
    $('view-summary').textContent=`${visible.length} tables/views · ${count} foreign keys`;
    if(fitView)fit();
  }
  function selectTable(id,center=true){selected=id;anchor=id;selectedEdge=null;render($('mode').value==='neighbours',$('mode').value==='neighbours');if(center&&$('mode').value!=='neighbours')centerTable(id);}
  function notice(text){$('notice').textContent=text;$('notice').classList.add('visible');clearTimeout(noticeTimer);noticeTimer=setTimeout(()=>$('notice').classList.remove('visible'),3000);}
  function download(content,type,name){
    const url=URL.createObjectURL(new Blob([content],{type})),a=document.createElement('a');a.href=url;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);notice(`Saved ${name}`);
  }
  $('svg-export').onclick=()=>{
    calculateBounds();const copy=svg.cloneNode(true);copy.setAttribute('viewBox',`${bounds.x} ${bounds.y} ${bounds.w} ${bounds.h}`);copy.setAttribute('width',String(Math.ceil(bounds.w)));copy.setAttribute('height',String(Math.ceil(bounds.h)));
    copy.querySelectorAll('.dim').forEach(el=>el.classList.remove('dim'));
    copy.insertBefore(make('rect',{x:bounds.x,y:bounds.y,width:bounds.w,height:bounds.h,fill:'#0b1421'}),copy.firstChild);
    download('<?xml version="1.0" encoding="UTF-8"?>\n'+new XMLSerializer().serializeToString(copy),'image/svg+xml','schema.svg');
  };
  const safeLabel=s=>String(s).replace(/["\r\n\u0000-\u001f]/g,' ').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  $('mermaid-export').onclick=()=>{
    const lines=['erDiagram','    %% Declared relationships; PK/FK/UK groups are detailed in the HTML snapshot.'];
    const ids=new Set(visible.map(t=>t.id));
    for(const t of visible){
      lines.push(`    ${t.id}["${safeLabel(fullName(t))}"] {`);
      shownColumns(t).forEach((c,i)=>{
        const type=c.data_type.replace(/[^A-Za-z0-9_\[\]]/g,'_').replace(/^[^A-Za-z]+/,'')||'unknown';
        const name=`c${i}_${c.name.replace(/[^A-Za-z0-9_]/g,'_')}`;
        const marks=flags(t,c),comment=safeLabel(`${c.name} · ${c.data_type}${c.nullable?' · nullable':' · NOT NULL'}`);
        lines.push(`        ${type} ${name}${marks.length?' '+marks.join(','):''} "${comment}"`);
      });lines.push('    }');
    }
    for(const f of links)if(ids.has(f.source)&&ids.has(f.target))lines.push(`    ${f.target} ${f.optional?'|o':'||'}..${f.unique?'o|':'o{'} ${f.source} : "${safeLabel(f.name+': '+f.pairs.map(p=>p.join(' → ')).join(', '))}"`);
    download(lines.join('\n')+'\n','text/plain;charset=utf-8','schema.mmd');
  };
  $('html-export').onclick=()=>download(sourceHTML,'text/html;charset=utf-8','schema.html');
  // Pointer capture keeps table dragging/panning active outside the canvas.
  svg.onpointerdown=e=>{
    if(e.button!==0||e.target.closest('[data-edge]'))return;
    const table=e.target.closest('[data-table]'),id=table?.getAttribute('data-table');
    drag={id,startX:e.clientX,startY:e.clientY,lastX:e.clientX,lastY:e.clientY,moved:false};
    svg.setPointerCapture(e.pointerId);svg.classList.add('dragging');
    if(id){selected=id;selectedEdge=null;highlight();details();buildList();}
  };
  svg.onpointermove=e=>{
    if(!drag)return;const dx=(e.clientX-drag.lastX)*camera.w/Math.max(1,svg.clientWidth),dy=(e.clientY-drag.lastY)*camera.h/Math.max(1,svg.clientHeight);
    drag.moved ||= Math.abs(e.clientX-drag.startX)+Math.abs(e.clientY-drag.startY)>4;
    if(drag.id){const p=positions.get(drag.id);p.x+=dx;p.y+=dy;const el=[...$('tables').children].find(n=>n.getAttribute('data-table')===drag.id);el.setAttribute('transform',`translate(${p.x} ${p.y})`);drawEdges();highlight();}
    else{camera.x-=dx;camera.y-=dy;applyCamera();}
    drag.lastX=e.clientX;drag.lastY=e.clientY;
  };
  function endDrag(e){
    if(!drag)return;const saved=drag;drag=null;svg.classList.remove('dragging');if(svg.hasPointerCapture(e.pointerId))svg.releasePointerCapture(e.pointerId);
    if(saved.id){if(!saved.moved&&$('mode').value==='neighbours')selectTable(saved.id,false);else{drawClusters();calculateBounds();}}
    else if(!saved.moved){selected=null;selectedEdge=null;highlight();details();buildList();}
  }
  svg.onpointerup=endDrag;svg.onpointercancel=endDrag;
  svg.onlostpointercapture=()=>{drag=null;svg.classList.remove('dragging');};
  svg.addEventListener('wheel',e=>{e.preventDefault();const rect=svg.getBoundingClientRect();zoom(Math.exp(Math.max(-.5,Math.min(.5,e.deltaY*.0015))),(e.clientX-rect.left)/rect.width,(e.clientY-rect.top)/rect.height);},{passive:false});
  $('fit').onclick=fit;$('layout').onclick=()=>render(true,true);$('zoom-in').onclick=()=>zoom(.8);$('zoom-out').onclick=()=>zoom(1.25);
  $('search').oninput=()=>{buildList();highlight();};
  $('schema').onchange=()=>{$('mode').value='all';selected=null;selectedEdge=null;render(true,true);};
  $('mode').onchange=()=>{if($('mode').value==='neighbours'&&!selected){$('mode').value='all';notice('Select a table first.');return;}anchor=selected;render(true,true);};
  $('keys-only').onchange=()=>render(true,true);
  $('help-open').onclick=()=>$('help').showModal();$('help-close').onclick=()=>$('help').close();
  $('details-toggle').onclick=()=>document.body.classList.toggle('show-details');$('details-close').onclick=()=>document.body.classList.remove('show-details');
  document.addEventListener('keydown',e=>{
    if(e.ctrlKey||e.metaKey||e.altKey)return;
    if(['INPUT','SELECT','TEXTAREA','BUTTON'].includes(document.activeElement.tagName)||$('help').open)return;
    if(e.key==='/'){e.preventDefault();$('search').focus();}
    else if(e.key.toLowerCase()==='f')fit();
    else if(e.key==='+'||e.key==='=')zoom(.8);else if(e.key==='-')zoom(1.25);
    else if(e.key==='Escape'){selected=null;selectedEdge=null;render();}
    else if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(e.key)){e.preventDefault();camera.x+=e.key==='ArrowLeft'?-camera.w*.08:e.key==='ArrowRight'?camera.w*.08:0;camera.y+=e.key==='ArrowUp'?-camera.h*.08:e.key==='ArrowDown'?camera.h*.08:0;applyCamera();}
  });
  for(const schema of schemas){const option=document.createElement('option');option.value=schema;option.textContent=schema;$('schema').append(option);}
  $('summary').textContent=`${data.engine==='postgres'?'PostgreSQL':'SQLite'} · ${data.tables.length} tables/views · ${links.length} foreign keys · offline snapshot`;
  document.title=`${data.engine==='postgres'?'PostgreSQL':'SQLite'} schema · dbcli`;
  render(true,true);new ResizeObserver(()=>fit()).observe($('canvas'));
})();
