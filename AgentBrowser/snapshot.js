((limits={}) => {
  if (!document.body) return null;
  const controlLimit=limits.max_controls_inspected ?? 5000;
  const optionLimit=limits.max_options_inspected ?? 5000;
  const regionLimit=limits.max_regions ?? 32;
  const cache = window.__jevFast ||= {ids:new WeakMap(), nodes:new Map(), next:1};
  const identity = e => {
    if (!cache.ids.has(e)) cache.ids.set(e,cache.next++);
    const id=cache.ids.get(e); cache.nodes.set(id,e); return id;
  };
  // Keep stable WeakMap identities without retaining every node seen in earlier traversals.
  cache.nodes.clear();
  const safe = e => !['password','file','hidden'].includes(e.type);
  const visible = e => !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  const name = (e,seen=new Set()) => {
    if (!e || seen.has(e)) return '';
    seen.add(e);
    const referenced=(e.getAttribute('aria-labelledby')||'').split(/\s+/)
      .map(id=>name(document.getElementById(id),seen)).filter(Boolean).join(' ');
    return referenced || e.getAttribute('aria-label') ||
      [...(e.labels||[])].map(l=>name(l,seen)).filter(Boolean).join(' ') ||
      (['button','submit','reset'].includes(e.type) ? e.value : '') || e.getAttribute('alt') ||
      // A native selector's alternatives are inspected separately under their own budget.
      (['INPUT','SELECT'].includes(e.tagName) ? '' : [...e.childNodes].map(n=>n.nodeType===3 ? n.textContent :
        n.nodeType===1 && n.getAttribute('aria-hidden')!=='true' ? name(n,seen) : '').join(' ').trim()) ||
      e.getAttribute('title') || e.getAttribute('placeholder') || '';
  };
  const roles=['button','link','checkbox','radio','switch','tab','menuitem','menuitemradio',
    'option','gridcell','combobox','textbox','searchbox','spinbutton'];
  const selector='a[href],button,input,textarea,select,summary,[contenteditable="true"],'+
    roles.map(role=>'[role="'+role+'"]').join(',');
  const regionSelector='dialog,[role="dialog"],form,tr,[role="row"],article,li';
  const contextSelector='dialog,[role="dialog"],form,fieldset,[role="region"],[role="group"],'+
    '[role="listbox"],[role="menu"],section,article,nav,aside,main';
  const headingSelector='h1,h2,h3,h4,h5,h6,[role="heading"],legend';
  const contexts=new Map(), scrollRoots=new Map(), contextControllers=new Map();
  const boundedText = e => (e?.innerText || e?.textContent || '').replace(/\s+/g,' ').trim().slice(0,160);
  const context = e => {
    let root=e.closest(contextSelector);
    for (let ancestor=e,depth=0; ancestor && depth<24; ancestor=ancestor.parentElement,depth++) {
      if (contextControllers.has(ancestor)) { root=ancestor; break; }
      if (ancestor===root) break;
    }
    // Many custom panels omit roles. A nearby heading is still observed evidence of a group.
    if (root!==e) {
      let parent=e.parentElement;
      for (let depth=0; parent && parent!==document.body && depth<12; depth++,parent=parent.parentElement) {
        if ([...parent.children].slice(0,32).some(child=>child.matches(headingSelector))) {
          root=parent; break;
        }
        if (parent===root) break;
      }
    }
    root ||= e.closest('ul,ol,[role="list"]') || document.body;
    const key='r'+identity(root);
    if (contexts.has(key)) return contexts.get(key);
    if (contexts.size>=regionLimit) return {id:'residual',kind:'group',heading:''};
    const labelled=(root.getAttribute('aria-labelledby')||'').split(/\s+/)
      .map(id=>boundedText(document.getElementById(id))).filter(Boolean).join(' ').slice(0,160);
    const controllers=contextControllers.get(root) || [];
    const controller=controllers.length===1 ? controllers[0] : null;
    const descriptor={id:key,kind:root.getAttribute('role') ||
      ({BODY:'document',DIALOG:'dialog',FORM:'form',FIELDSET:'group',UL:'list',OL:'list'}[root.tagName] || 'region'),
    heading:labelled || root.getAttribute('aria-label')?.slice(0,160) ||
      (controller ? label(controller).slice(0,160) : '') ||
      boundedText(root.querySelector(headingSelector))};
    contexts.set(key,descriptor); return descriptor;
  };
  const viewportRect = e => {
    const r=e.getBoundingClientRect();
    // CSS containing blocks, absolute positioning and top-layer elements make ancestor
    // overflow insufficient to determine clipping. Let the browser hit-test actual pixels.
    const left=Math.max(0,r.left),top=Math.max(0,r.top),right=Math.min(innerWidth,r.right),bottom=Math.min(innerHeight,r.bottom);
    return {left,top,right,bottom,width:right-left,height:bottom-top};
  };
  const visibleRange = (range,parent) => [...range.getClientRects()].slice(0,32).some(r=>{
    const left=Math.max(0,r.left),top=Math.max(0,r.top),right=Math.min(innerWidth,r.right),bottom=Math.min(innerHeight,r.bottom);
    if (right<=left || bottom<=top) return false;
    return [.5,.2,.8].some(fraction=>parent.contains(document.elementFromPoint(
      left+(right-left)*fraction,top+(bottom-top)/2)));
  });
  const scrollState = e => [identity(e),e.scrollTop,e.scrollLeft,e.clientHeight,e.clientWidth,e.scrollHeight,e.scrollWidth];
  const scrollPoint = e => {
    const documentRoot=e===document.scrollingElement;
    const r=documentRoot ? {left:0,top:0,width:innerWidth,height:innerHeight} : viewportRect(e);
    if (r.width<8 || r.height<8) return null;
    for (const [fx,fy] of [[.5,.5],[.8,.5],[.2,.5],[.5,.8],[.5,.2]]) {
      const x=r.left+r.width*fx,y=r.top+r.height*fy,hit=document.elementFromPoint(x,y);
      if (!hit || (!documentRoot && !e.contains(hit))) continue;
      // A wheel above a nested scroll container would act on that container instead.
      let nested=false;
      for (let child=hit; child && child!==e && child!==document.body; child=child.parentElement) {
        if (child.scrollHeight>child.clientHeight+2 && /auto|scroll/.test(getComputedStyle(child).overflowY)) {
          nested=true; break;
        }
      }
      if (!nested) return {x,y};
    }
    return null;
  };
  cache.scrollPoint=scrollPoint;
  const role = e => {
    const explicit=e.getAttribute('role');
    if (roles.includes(explicit)) return explicit;
    if (e.tagName==='BUTTON' || e.tagName==='SUMMARY') return 'button';
    if (e.tagName==='A') return 'link';
    if (e.tagName==='SELECT') return 'combobox';
    if (e.tagName==='TEXTAREA' || e.isContentEditable) return 'textbox';
    if (e.tagName==='INPUT') {
      if (['checkbox','radio'].includes(e.type)) return e.type;
      if (['button','submit','reset','image'].includes(e.type)) return 'button';
      if (e.type==='search') return 'searchbox';
      if (e.type==='number') return 'spinbutton';
      if (['text','email','url','tel'].includes(e.type)) return 'textbox';
    }
    return null;
  };
  // Some custom toggles put their text beside an otherwise unnamed icon button.
  // Use only a small, unambiguous semantic row; never borrow an entire form's text.
  const labelScopes='li,tr,[role="listitem"],[role="row"],[role="option"],label';
  const contextualName = e => {
    const scope=e.parentElement?.closest(labelScopes);
    if (!scope) return '';
    const after = node => {
      while (node && node!==scope) {
        if (node.nextSibling) return node.nextSibling;
        node=node.parentNode;
      }
      return null;
    };
    const words=[], range=document.createRange();
    let cursor=scope.firstChild, inspected=0, length=0, rawLength=0, found=false;
    while (cursor) {
      if (++inspected>200) return '';
      const node=cursor;
      cursor=node.firstChild || after(node);
      if (node.nodeType===1) {
        if (node.matches('script,style,noscript,template,svg') || !visible(node)) {
          cursor=after(node); continue;
        }
        if (node.matches(selector) && role(node)) {
          if (node!==e) return '';
          found=true; cursor=after(node); continue;
        }
        if (node.matches(labelScopes)) return '';
      } else if (node.nodeType===3) {
        const raw=node.textContent;
        rawLength+=raw.length;
        if (rawLength>4096) return '';
        const text=raw.replace(/\s+/g,' ').trim();
        if (!text) continue;
        range.selectNodeContents(node);
        if (!visibleRange(range,node.parentElement)) continue;
        length+=text.length+1;
        if (length>240) return '';
        words.push(text);
      }
    }
    return found ? words.join(' ') : '';
  };
  const label = e => name(e).trim() || (role(e)==='button' ? contextualName(e) : '');
  // Explicit relationships can name a role-less popup without borrowing unrelated page text.
  for (const controller of [...document.querySelectorAll('[aria-controls],[aria-owns]')].slice(0,controlLimit)) {
    if (!visible(controller)) continue;
    const ids=new Set(((controller.getAttribute('aria-controls')||'')+' '+
      (controller.getAttribute('aria-owns')||'')).split(/\s+/).filter(Boolean));
    for (const id of [...ids].slice(0,16)) {
      const target=document.getElementById(id);
      if (!target || target.contains(controller)) continue;
      if (!contextControllers.has(target)) contextControllers.set(target,[]);
      contextControllers.get(target).push(controller);
    }
  }
  const nodes=[], controls=[], optionStates=[], regions=new Map(), unsupported=new Set(), unsupportedDetails=[];
  let unsupportedHosts=0;
  const inspectUnsupportedHost = (e,kind) => {
    unsupportedHosts++;
    if (unsupportedDetails.length>=regionLimit) return;
    const r=e.getBoundingClientRect(), clipped=viewportRect(e);
    const inViewport=clipped.width>0 && clipped.height>0;
    const cssVisible=e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
    const reachable=cssVisible && inViewport && [[.5,.5],[.2,.2],[.8,.8]].some(([fx,fy])=>
      e.contains(document.elementFromPoint(clipped.left+clipped.width*fx,clipped.top+clipped.height*fy)));
    const number=value=>Number.isFinite(value) ? Math.max(-10000000,Math.min(10000000,Math.round(value))) : 0;
    unsupportedDetails.push({kind,title:(e.getAttribute('title')||'').slice(0,160),
      label:(e.getAttribute('aria-label')||'').slice(0,160),css_visible:cssVisible,
      in_viewport:inViewport,reachable_sample:reachable,
      rect:{x:number(r.x),y:number(r.y),w:number(r.width),h:number(r.height)}});
  };
  // Skip select subtrees here: the separate bounded option loop is their sole inspection pass.
  const afterSubtree = node => {
    while (node && node!==document.body) {
      if (node.nextElementSibling) return node.nextElementSibling;
      node=node.parentElement;
    }
    return null;
  };
  let cursor=document.body.firstElementChild;
  let controlsInspected=0,optionsInspected=0,controlsComplete=true,optionsComplete=true,regionOverflow=false;
  const region = e => {
    const root=e.closest(regionSelector);
    const key=root ? 'r'+identity(root) : 'root';
    if (regions.has(key)) return key;
    if (regions.size>=regionLimit) { regionOverflow=true; return 'residual'; }
    regions.set(key,true); return key;
  };
  while (cursor) {
    const e=cursor;
    cursor=(e.tagName==='SELECT' ? null : e.firstElementChild) || afterSubtree(e);
    if (['IFRAME','FRAME'].includes(e.tagName)) { unsupported.add('iframes'); inspectUnsupportedHost(e,'iframe'); }
    if (e.tagName==='CANVAS') { unsupported.add('canvas'); inspectUnsupportedHost(e,'canvas'); }
    if (e.shadowRoot) { unsupported.add('shadow_roots'); inspectUnsupportedHost(e,'shadow_root'); }
    if (scrollRoots.size<regionLimit && e.clientHeight>16 && e.scrollHeight>e.clientHeight+2 &&
        /auto|scroll/.test(getComputedStyle(e).overflowY) && visible(e) && scrollPoint(e))
      scrollRoots.set(identity(e),e);
    if (!e.matches(selector)) continue;
    if (controlsInspected>=controlLimit) { controlsComplete=false; break; }
    controlsInspected++;
    if (!safe(e)) continue;
    if (['INPUT','TEXTAREA','SELECT'].includes(e.tagName))
      controls.push([identity(e),e.value,e.checked,e.selectedIndex,e.disabled,e.readOnly]);
    if (!visible(e) || e.matches(':disabled') || e.closest('[aria-disabled="true"]')) continue;
    const r=e.getBoundingClientRect(), clipped=viewportRect(e), x=r.x+r.width/2, y=r.y+r.height/2, rname=role(e);
    if (!rname || clipped.width<=0 || clipped.height<=0 || x<clipped.left || y<clipped.top ||
        x>=clipped.right || y>=clipped.bottom) continue;
    if (!e.contains(document.elementFromPoint(x,y))) continue;
    if (rname==='gridcell' && e.querySelector('button,[role="button"]')) continue;
    const base={node:identity(e),region:region(e),context:context(e),role:rname,label:label(e)||rname,
      rect:{x:r.x,y:r.y,w:r.width,h:r.height}};
    // A collapsed disclosure can also be a native submit button. Resolve the
    // actual form owner, including external controls associated by form="id".
    if (['BUTTON','INPUT'].includes(e.tagName))
      base.submits_form=!!e.form && ['submit','image'].includes(e.type);
    if (rname==='link' && e.hasAttribute('href')) {
      try {
        const href=new URL(e.getAttribute('href'),document.baseURI).href;
        base.href=href.slice(0,2000);
        if (href.length>2000) base.href_truncated=true;
      } catch { /* A malformed URL is not evidence of a navigable destination. */ }
    }
    if ('required' in e || e.hasAttribute('aria-required')) base.required=e.required || e.getAttribute('aria-required')==='true';
    if ('validity' in e || e.hasAttribute('aria-invalid')) base.invalid=e.validity?.valid===false || e.getAttribute('aria-invalid')==='true';
    if (e.validationMessage) base.validation_message=e.validationMessage.slice(0,240);
    if ('readOnly' in e) base.readonly=e.readOnly;
    for (const key of ['checked','selected','expanded','pressed']) {
      const value=e.getAttribute('aria-'+key);
      if (value!==null) base[key]=value;
    }
    if (['checkbox','radio'].includes(e.type)) base.checked=String(e.checked);
    if (e.tagName==='SELECT') {
      base.operations=['select']; base.options=[];
      const selected=[]; let inspected=0;
      // Do not expand every option into an action, or materialize an unbounded options array.
      for (let index=0; index<e.options.length; index++) {
        if (optionsInspected>=optionLimit) { optionsComplete=false; break; }
        const o=e.options[index], option=identity(o), disabled=o.disabled || !!o.closest('optgroup[disabled]');
        optionsInspected++; inspected++;
        optionStates.push([base.node,option,index,o.label,o.value,o.selected,disabled]);
        if (o.selected) selected.push(o.label);
        if (!o.selected && !disabled) base.options.push({option,value:o.value,label:o.label});
      }
      base.current_value=selected.join(', ');
      base.options_inspected=inspected;
      base.options_complete=inspected===e.options.length;
    } else {
      const editable=!e.readOnly && e.getAttribute('aria-readonly')!=='true' &&
        (['textbox','searchbox','spinbutton'].includes(rname) ||
          (rname==='combobox' && ['INPUT','TEXTAREA'].includes(e.tagName)));
      base.value='value' in e ? String(e.value) :
        e.isContentEditable || rname==='combobox' ? e.innerText.trim() : '';
      base.operations=editable ? ['fill','click'] : ['click'];
    }
    nodes.push(base);
  }
  const visibleText = root => {
    const words=[], textWalker=document.createTreeWalker(root,NodeFilter.SHOW_ELEMENT|NodeFilter.SHOW_TEXT,{
      acceptNode:node=>node.nodeType===1 ?
        (node.matches('script,style,noscript,template,select') ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_SKIP) :
        NodeFilter.FILTER_ACCEPT,
    });
    const range=document.createRange(); let node,length=0;
    while ((node=textWalker.nextNode()) && length<6000) {
      const value=node.textContent.trim(), parent=node.parentElement;
      if (!value || !parent || !visible(parent)) continue;
      range.selectNodeContents(node);
      if (visibleRange(range,parent)) {
        words.push(value); length+=value.length;
      }
    }
    return words.join('\n').slice(0,6000);
  };
  const text=visibleText(document.body), height=document.documentElement.scrollHeight;
  const page_key=[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,controls];
  cache.pageKey=()=>page_key;
  const scopeTexts=new Map();
  cache.guard=e=>{
    if (!e?.isConnected || !visible(e)) return null;
    if (scrollRoots.has(identity(e)) || e===document.scrollingElement)
      return ['scroll',...scrollState(e),context(e)];
    const scope=e.closest(regionSelector) || e.parentElement;
    const scopeId=scope ? identity(scope) : null;
    if (scopeId!==null && !scopeTexts.has(scopeId)) scopeTexts.set(scopeId,visibleText(scope));
    return [identity(e),role(e),label(e),e.value??null,e.checked??null,e.selectedIndex??null,
      e.readOnly??null,e.matches(':disabled'),e.getAttribute('aria-disabled'),
      e.getAttribute('aria-expanded'),e.getAttribute('aria-checked'),e.getAttribute('aria-selected'),
      e.getAttribute('aria-pressed'),
      e.getAttribute('href'),scopeId,context(e),
      ['BUTTON','INPUT'].includes(e.tagName) ? [e.type,e.form ? identity(e.form) : null] : null];
  };
  const guards={};
  for (const n of nodes) guards[n.node]=cache.guard(cache.nodes.get(n.node));
  const globals=[];
  const documentRoot=document.scrollingElement;
  if (documentRoot && documentRoot.scrollHeight>innerHeight+2 && scrollPoint(documentRoot))
    scrollRoots.set(identity(documentRoot),documentRoot);
  for (const [node,e] of scrollRoots) {
    const c=context(e), documentScroll=e===documentRoot, y=documentScroll ? scrollY : e.scrollTop;
    const viewport=documentScroll ? innerHeight : e.clientHeight, total=e.scrollHeight;
    const descriptor=documentScroll ? 'Page' : c.heading || e.getAttribute('aria-label') || 'Scrollable region';
    const base={kind:'scroll',node,region:'r'+node,context:c,role:'region',position:y,scroll:{y,height:total,viewport}};
    if (y+viewport<total-2) globals.push({...base,id:'scroll_'+node+'_down',direction:'down',
      label:descriptor+' — scroll down',delta:Math.max(40,Math.round(viewport*.75))});
    if (y>0) globals.push({...base,id:'scroll_'+node+'_up',direction:'up',
      label:descriptor+' — scroll up',delta:-Math.max(40,Math.round(viewport*.75))});
    guards[node]=cache.guard(e);
  }
  globals.push({id:'wait',kind:'wait',label:'Wait for the page to update'});
  globals.push({id:'reload',kind:'reload',label:'Refresh the current page'});
  const coverage={
    scope:'top-document-visible-controls',controls_inspected:controlsInspected,options_inspected:optionsInspected,
    controls_limit:controlLimit,options_limit:optionLimit,controls_total_known:controlsComplete,
    options_total_known:optionsComplete && controlsComplete,
    extraction_complete:controlsComplete && optionsComplete && unsupported.size===0,
    regions:regions.size,region_overflow:regionOverflow,unsupported_contexts:[...unsupported],
    unsupported_contexts_complete:controlsComplete,
    unsupported_context_details:unsupportedDetails,
    unsupported_context_details_complete:controlsComplete && unsupportedHosts<=regionLimit,
    unsupported_context_details_limit:regionLimit,unsupported_hosts_observed:unsupportedHosts,
    excluded_contexts:['offscreen_controls','covered_or_clipped_control_centres','frame_contents','shadow_root_contents','closed_shadow_roots',
      'canvas_contents','native_browser_controls','other_tabs'],
    scroll_regions:scrollRoots.size,
    discovery_available:[...(globals.some(a=>a.kind==='scroll') ? ['scroll'] : [])],
    reasons:[...(!controlsComplete ? ['control_inspection_limit'] : []),
      ...(!optionsComplete ? ['option_inspection_limit'] : []),...(unsupported.size ? ['unsupported_contexts'] : [])],
  };
  const inventory={version:1,nodes,globals,contexts:[...contexts.values()],coverage};
  // Geometry is resolved immediately before input. All pending candidates participate in freshness.
  const semantics=nodes.map(({rect,...n})=>n);
  const inventory_marker=[...page_key.slice(0,6),semantics,optionStates,controls,
    Object.entries(guards),[...scopeTexts],globals,coverage];
  const busy=[...document.querySelectorAll('[aria-busy="true"]')].slice(0,32).some(e=>{
    const r=viewportRect(e); return visible(e) && r.width>0 && r.height>0;
  });
  const empty=!text.trim() && nodes.length===0;
  const readiness={status:document.readyState==='loading' || busy ? 'loading' : empty ? 'unknown' : 'ready',
    reason:document.readyState==='loading' ? 'document_loading' : busy ? 'aria_busy' : empty ? 'empty_observation' : 'content_available',
    document_state:document.readyState};
  const marker=[inventory_marker,document.title,text,height];
  return {url:location.href,title:document.title,w:innerWidth,h:innerHeight,text,
    scroll:{y:scrollY,height},contexts:inventory.contexts,readiness,inventory,inventory_marker,marker,page_key,guards};
})(/*__JEV_LIMITS__*/)
