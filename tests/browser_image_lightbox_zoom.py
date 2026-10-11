#!/usr/bin/env python3
"""Agent-free real-server image/Mermaid viewer browser gate.

Run with Python + Playwright: --engine chromium|webkit|all, --repo PATH.
The optional --shots DIR captures the same generated 2400px detailed map at
fit, 3x and an edge; --before captures the unchanged base without zoom checks.
Only Chromium's trusted CDP two-contact pinch is skipped on WebKit. Portable
touch-event dispatch also verifies the single-contact/swipe/double-tap paths;
it exercises DOM event handlers, not an OS-level WebKit touch injection.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

FIXTURE = r"""async () => {
  const panel = document.querySelector('#msgInner');
  panel.replaceChildren();
  const row = document.createElement('div'); row.className = 'msg-row';
  row.id = 'zoom-fixture';
  for(let n=0;n<2;n++){
    const c=document.createElement('canvas'); c.width=2400; c.height=1600;
    const g=c.getContext('2d');
    g.fillStyle=n?'#132a35':'#172438';g.fillRect(0,0,2400,1600);
    g.fillStyle='#edf4f8';g.font='bold 64px sans-serif';
    g.fillText(n?'COASTAL OBSERVATORY / 02':'ORBITAL SURVEY / 01',80,105);
    g.font='26px sans-serif';g.fillStyle='#92b2c6';
    g.fillText('SYNTHETIC ATLAS  •  GRID 2400 × 1600  •  NO PERSONAL DATA',80,155);
    for(let y=220;y<1500;y+=110)for(let x=80;x<2320;x+=140){
      g.strokeStyle='#496078';g.lineWidth=2;g.strokeRect(x,y,118,82);
      g.fillStyle=['#549baf','#e4bb77','#788cc5'][(x+y+n)%3];
      g.beginPath();g.arc(x+25,y+25,10+(x%7),0,Math.PI*2);g.fill();
      g.font='18px monospace';g.fillStyle='#d8e7f0';g.fillText(`${x/10}:${y/10}`,x+10,y+65);
      g.strokeStyle='#638094';g.beginPath();g.moveTo(x+40,y+25);g.lineTo(x+110,y+25);g.stroke();
    }
    g.strokeStyle='#72cfbf';g.lineWidth=5;g.beginPath();
    for(let x=0;x<2400;x+=8){const y=800+200*Math.sin(x/180)+80*Math.cos(x/77);if(!x)g.moveTo(x,y);else g.lineTo(x,y);}g.stroke();
    const img=document.createElement('img');img.className='msg-media-img';
    img.alt=n?'Coastal observatory detailed atlas':'Orbital survey detailed atlas';
    img.src=c.toDataURL('image/png');row.appendChild(img);await img.decode();
  }
  panel.appendChild(row);
}"""

SNAP = r"""() => {
  const lb=document.querySelector('.img-lightbox');
  const s=lb._imageViewer, im=lb.querySelector('img'), r=im.getBoundingClientRect();
  const v=s.viewport.getBoundingClientRect();
  return {scale:s.scale,fit:s.fitScale,x:s.x,y:s.y,index:lb._navIndex,
    left:r.left,top:r.top,width:r.width,height:r.height,nw:im.naturalWidth,nh:im.naturalHeight,
    vl:v.left,vt:v.top,vw:v.width,vh:v.height,alt:im.alt,label:lb.getAttribute('aria-label')};
}"""


def wait(page, expression, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if page.evaluate(expression):
            return
        page.wait_for_timeout(50)
    raise AssertionError(f'timed out: {expression}')


def snap(page):
    return page.evaluate(SNAP)


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def fit(page):
    page.get_by_role('button', name='Fit to screen', exact=True).click()
    s = snap(page)
    check(abs(s['scale'] - s['fit']) < 1e-8, 'fit/reset scale')
    return s


def open_image(page):
    page.locator('#zoom-fixture img').first.click()
    wait(page, "!!document.querySelector('.img-lightbox img')?.naturalWidth")
    page.wait_for_timeout(180)


def touch(page, start, end=None, cancel=False):
    if getattr(page, '_chromium', False):
        session = page.context.new_cdp_session(page)
        def point(p):
            return [{'x':p[0],'y':p[1],'id':1}]
        session.send('Input.dispatchTouchEvent', {'type':'touchStart','touchPoints':point(start)})
        if end:
            session.send('Input.dispatchTouchEvent', {'type':'touchMove','touchPoints':point(end)})
        session.send('Input.dispatchTouchEvent', {'type':'touchCancel' if cancel else 'touchEnd','touchPoints':[]})
        session.detach()
        return
    # WebKit Playwright exposes taps but no continuous multi-contact input.
    # Real DOM touch events cover the portable handlers; CDP below adds trusted
    # browser-dispatched input on Chromium rather than pretending this is native.
    page.evaluate(r"""({start,end,cancel})=>{
      const v=document.querySelector('.img-zoom-viewport');
      const t=(p)=>({identifier:1,target:v,clientX:p[0],clientY:p[1],pageX:p[0],pageY:p[1]});
      const fire=(type,p,done=false)=>{
        const e=new Event(type,{bubbles:true,cancelable:true});
        Object.defineProperties(e,{touches:{value:done?[]:[t(p)]},targetTouches:{value:done?[]:[t(p)]},changedTouches:{value:[t(p)]}});
        return v.dispatchEvent(e);
      };
      fire('touchstart',start); if(end)fire('touchmove',end);
      fire(cancel?'touchcancel':'touchend',end||start,true);
    }""", {'start': start, 'end': end, 'cancel': cancel})


def wheel(page, x, y, delta=-250, ctrl=False):
    if getattr(page, '_mobile_webkit', False):
        # Playwright explicitly refuses mouseWheel on mobile WebKit. The
        # desktop WebKit context above exercises trusted wheel; here dispatch
        # the same cancelable DOM event to cover its mobile listener as well.
        page.evaluate("""({x,y,delta,ctrl})=>document.querySelector('.img-zoom-viewport').dispatchEvent(
          new WheelEvent('wheel',{bubbles:true,cancelable:true,clientX:x,clientY:y,deltaY:delta,ctrlKey:ctrl}))""",
          {'x':x,'y':y,'delta':delta,'ctrl':ctrl})
        return
    page.mouse.move(x, y)
    if ctrl:
        page.keyboard.down('Control')
    page.mouse.wheel(0, delta)
    page.wait_for_timeout(100)
    if ctrl:
        page.keyboard.up('Control')


def zoom_checks(page, browser, engine):
    open_image(page)
    check(page.locator('.img-zoom-toolbar button').count() == 3, 'visible shared zoom controls missing')
    s = snap(page)
    check(s['nw'] == 2400 and s['alt'] == s['label'], 'natural size / alt / dialog label')
    # Use a near-centre anchor, away from bounds, to measure the rendered pixel.
    ax, ay = s['vl'] + s['vw'] * .53, s['vt'] + s['vh'] * .5
    u, v = (ax-s['left'])/s['width'], (ay-s['top'])/s['height']
    wheel(page, ax, ay)
    z = snap(page)
    check(z['scale'] > s['scale'], 'wheel zoom')
    check(abs(z['left']+u*z['width']-ax) <= 2 and abs(z['top']+v*z['height']-ay) <= 2,
          'wheel must preserve the rendered point under the cursor within 2px')
    before = page.evaluate('({x:scrollX,y:scrollY,scale:visualViewport.scale,m:document.querySelector("#messages")?.scrollTop})')
    wheel(page, ax, ay, ctrl=True)
    after = page.evaluate('({x:scrollX,y:scrollY,scale:visualViewport.scale,m:document.querySelector("#messages")?.scrollTop})')
    check(before == after and snap(page)['scale'] > z['scale'], 'ctrl-wheel must zoom image, not page')
    prevented = page.evaluate("""()=>{const e=new WheelEvent('wheel',{bubbles:true,cancelable:true,ctrlKey:true,deltaY:-1});
      document.querySelector('.img-zoom-viewport').dispatchEvent(e);return e.defaultPrevented;}""")
    check(prevented, 'wheel preventDefault')
    outside=snap(page)
    backdrop=page.evaluate("""()=>{const lb=document.querySelector('.img-lightbox');const e=new WheelEvent('wheel',
      {bubbles:true,cancelable:true,ctrlKey:true,deltaY:-50,clientX:3,clientY:3});lb.dispatchEvent(e);return e.defaultPrevented;}""")
    check(backdrop and snap(page)['scale']>outside['scale'],'ctrl-wheel on backdrop zooms image, not page')
    check(page.evaluate("getComputedStyle(document.querySelector('.img-zoom-viewport')).touchAction") == 'none', 'touch-action')
    for _ in range(6):
        old = snap(page)
        if old['height'] > old['vh'] + 100:
            break
        wheel(page, ax, ay, delta=-600)
    old = snap(page)
    page.mouse.move(ax, ay);page.mouse.down();page.mouse.move(ax-70, ay-60, steps=8);page.mouse.up()
    p = snap(page)
    check(p['x'] < old['x'] and p['y'] < old['y'], f'drag pans image: before={old}, after={p}')
    # Huge captured drags exercise both edges and ensure the pointer can leave.
    for dx, dy in ((6000,6000),(-6000,-6000)):
        page.mouse.move(ax, ay);page.mouse.down();page.mouse.move(ax+dx,ay+dy);page.mouse.up()
        p = snap(page)
        check(p['left'] <= p['vl']+1 and p['left']+p['width'] >= p['vl']+p['vw']-1, 'horizontal pan clamp')
        check(p['top'] <= p['vt']+1 and p['top']+p['height'] >= p['vt']+p['vh']-1, 'vertical pan clamp')
    fit(page)
    page.mouse.dblclick(ax, ay);page.wait_for_timeout(100)
    check(abs(snap(page)['width'] - 2400) <= 1, 'double-click native 1 CSS pixel per image pixel')
    page.mouse.dblclick(ax, ay);check(abs(snap(page)['scale']-snap(page)['fit'])<1e-8, 'double-click returns fit')
    # Zoom buttons and fit floor / ceiling.
    page.get_by_role('button',name='Zoom in',exact=True).click();check(snap(page)['scale']>s['fit'],'zoom in button')
    page.get_by_role('button',name='Zoom out',exact=True).click();check(abs(snap(page)['scale']-s['fit'])<1e-8,'zoom out button')
    wheel(page,ax,ay,delta=100000);check(abs(snap(page)['scale']-s['fit'])<1e-8,'fit floor')
    wheel(page,ax,ay,delta=-100000);check(abs(snap(page)['scale']-max(8*s['fit'],1))<1e-8,'max zoom')
    page.get_by_role('button',name='Next image',exact=True).click()
    wait(page,"document.querySelector('.img-lightbox img').alt.startsWith('Coastal')")
    page.wait_for_timeout(100);check(snap(page)['index']==1 and abs(snap(page)['scale']-snap(page)['fit'])<1e-8,'button navigation resets fit')
    page.get_by_role('button',name='Zoom in',exact=True).click();page.keyboard.press('ArrowLeft');page.wait_for_timeout(100)
    check(snap(page)['index']==0 and abs(snap(page)['scale']-snap(page)['fit'])<1e-8,'arrow navigation resets fit')
    s=fit(page); ax=s['vl']+s['vw']/2;ay=s['vt']+s['vh']/2
    touch(page,[ax+65,ay],[ax-65,ay]);page.wait_for_timeout(100)
    check(snap(page)['index']==1 and abs(snap(page)['scale']-snap(page)['fit'])<1e-8,'fit swipe navigation/reset')
    touch(page,[ax-65,ay],[ax+65,ay]);page.wait_for_timeout(100)
    check(snap(page)['index']==0,'reverse swipe')
    touch(page,[ax,ay]);touch(page,[ax,ay]);check(abs(snap(page)['scale']-1)<1e-8,'double-tap native size')
    touch(page,[ax,ay]);touch(page,[ax,ay]);check(abs(snap(page)['scale']-snap(page)['fit'])<1e-8,'double-tap fit')
    wheel(page,ax,ay,delta=-800);old=snap(page)
    touch(page,[ax,ay],[ax-80,ay-50]);p=snap(page)
    check(p['index']==0 and p['x']<old['x'],'zoomed one-finger drag pans, does not navigate')
    # Start at left edge: sidebar's capture recognizer must not steal a pan.
    page.evaluate("document.querySelector('.sidebar').classList.remove('mobile-open')")
    touch(page,[25,ay],[150,ay]);check(not page.evaluate("document.querySelector('.sidebar').classList.contains('mobile-open')"),'sidebar gesture collision')
    if engine == 'chromium':
        fit(page);s=snap(page)
        session=page.context.new_cdp_session(page)
        cx=s['vl']+s['vw']/2;cy=s['vt']+s['vh']/2
        u=(cx-s['left'])/s['width'];v=(cy-s['top'])/s['height']
        def dispatch(kind, dist=0):
            session.send('Input.dispatchTouchEvent', {'type':kind,'touchPoints':[] if kind=='touchEnd' else
              [{'x':cx-dist,'y':cy,'id':1},{'x':cx+dist,'y':cy,'id':2}]})
        dispatch('touchStart',35);dispatch('touchMove',90);dispatch('touchEnd');page.wait_for_timeout(100)
        p=snap(page)
        check(p['scale']>s['scale']*2,'trusted two-contact pinch zoom')
        check(abs(p['left']+u*p['width']-cx)<=2 and abs(p['top']+v*p['height']-cy)<=2,'pinch centre anchor')
        check(page.evaluate('visualViewport.scale')==1,'pinch must not page zoom')
        session.detach()
    else:
        print('SKIP webkit: CDP-specific trusted two-contact pinch only')
    # Drag terminating in real backdrop must not dismiss; a later click must.
    page.mouse.move(ax,ay);page.mouse.down();page.mouse.move(3,3,steps=8);page.mouse.up();page.wait_for_timeout(50)
    check(page.locator('.img-lightbox').count()==1,'drag ending on backdrop must not close')
    page.wait_for_timeout(550);page.mouse.click(3,3);wait(page,"!document.querySelector('.img-lightbox')")
    open_image(page);check(abs(snap(page)['scale']-snap(page)['fit'])<1e-8,'reopen fit')
    page.keyboard.press('Escape');wait(page,"!document.querySelector('.img-lightbox')")
    open_image(page);page.locator('.img-lightbox-close').click();wait(page,"!document.querySelector('.img-lightbox')")
    # Small and extremely large images exercise native target and >8x-fit cap.
    page.evaluate("""async()=>{const c=document.createElement('canvas');c.width=80;c.height=60;
      const i=document.querySelector('#zoom-fixture img');i.src=c.toDataURL();await i.decode();}""")
    open_image(page);s=snap(page);page.mouse.dblclick(s['vl']+s['vw']/2,s['vt']+s['vh']/2)
    check(abs(snap(page)['scale']-2)<1e-8,'small image fit toggles to 2x')
    fit(page);s=snap(page);page.wait_for_timeout(550)
    page.mouse.click(s['vl']+10,s['vt']+10)
    wait(page,"!document.querySelector('.img-lightbox')")
    page.evaluate(FIXTURE)
    gesture_lifecycle_checks(page)
    open_image(page)
    page.keyboard.press('Escape');wait(page,"!document.querySelector('.img-lightbox')")
    page.evaluate("""async()=>{const c=document.createElement('canvas');c.width=12000;c.height=1000;
      const i=document.querySelector('#zoom-fixture img');i.src=c.toDataURL();await i.decode();}""")
    open_image(page);s=snap(page);wheel(page,s['vl']+s['vw']/2,s['vt']+s['vh']/2,delta=-100000)
    check(abs(snap(page)['scale']-1)<1e-8,'large image can reach native size above 8x fit')
    page.keyboard.press('Escape');wait(page,"!document.querySelector('.img-lightbox')")
    page.evaluate(FIXTURE)
    print(f'OK {engine}: wheel/ctrl-wheel, bounds, mouse/touch toggles, swipe/pan, navigation, close, limits')


def contacts(page, kind, points, changed=None):
    """Portable contact-state sequences; Chromium also uses trusted CDP above."""
    page.evaluate(r"""({kind,points,changed})=>{
      const v=document.querySelector('.img-zoom-viewport');
      const ts=ps=>ps.map((p,i)=>({identifier:i+1,target:v,clientX:p[0],clientY:p[1]}));
      const e=new Event(kind,{bubbles:true,cancelable:true});
      Object.defineProperties(e,{touches:{value:ts(points)},changedTouches:{value:ts(changed||points)}});
      v.dispatchEvent(e);
    }""",{'kind':kind,'points':points,'changed':changed})


def gesture_lifecycle_checks(page):
    open_image(page);s=fit(page);cx=s['vl']+s['vw']/2;cy=s['vt']+s['vh']/2
    contacts(page,'touchstart',[[cx-35,cy]])
    contacts(page,'touchstart',[[cx-35,cy],[cx+35,cy]])
    contacts(page,'touchmove',[[cx-90+10,cy],[cx+90+10,cy]])
    z=snap(page)
    u=(cx-s['left'])/s['width'];v=(cy-s['top'])/s['height']
    check(abs(z['left']+u*z['width']-(cx+10))<=2 and abs(z['top']+v*z['height']-cy)<=2,'translating pinch midpoint')
    contacts(page,'touchend',[[cx+100,cy]],[[cx-80,cy]])
    contacts(page,'touchmove',[[cx+50,cy]])
    check(snap(page)['x']<z['x'],'pinch surviving finger resumes pan')
    contacts(page,'touchend',[],[[cx+50,cy]])
    check(snap(page)['index']==0,'pinch transition must not swipe')
    contacts(page,'touchstart',[[cx-35,cy],[cx+35,cy]])
    contacts(page,'touchcancel',[],[[cx-35,cy],[cx+35,cy]])
    check(page.evaluate("!document.querySelector('.img-lightbox')._imageViewer.pinching && !document.querySelector('.img-lightbox')._imageViewer.dragging"),'touchcancel releases gesture')
    old=snap(page);touch(page,[cx,cy],[cx-40,cy]);check(snap(page)['x']<old['x'],'pan works after cancellation')
    # Route an uncached replacement of a different aspect ratio. Hold a pinch
    # across navigation and deliver stale moves before the response is released.
    pending=[]
    page.route('**/zoom-delayed.svg',lambda route:pending.append(route))
    contacts(page,'touchstart',[[cx-35,cy],[cx+35,cy]])
    page.evaluate("""()=>{const lb=document.querySelector('.img-lightbox');
      lb._navImages[1]={src:location.origin+'/zoom-delayed.svg',alt:'Delayed portrait'};}""")
    page.keyboard.press('ArrowRight')
    deadline=time.monotonic()+5
    while not pending and time.monotonic()<deadline:page.wait_for_timeout(50)
    check(len(pending)==1,'delayed image request intercepted: '+str(page.evaluate("""()=>{const lb=document.querySelector('.img-lightbox');return {index:lb?._navIndex,src:lb?.querySelector('img').src,origin:location.origin,list:lb?._navImages.map(i=>i.src.slice(0,100))};}""")))
    s=snap(page)
    contacts(page,'touchmove',[[cx-100,cy],[cx+100,cy]])
    check(abs(snap(page)['scale']-s['fit'])<1e-8,'navigation invalidates old pinch before load')
    contacts(page,'touchend',[],[[cx-100,cy],[cx+100,cy]])
    pending.pop().fulfill(status=200,content_type='image/svg+xml',body='<svg xmlns="http://www.w3.org/2000/svg" width="600" height="1800"><rect width="600" height="1800" fill="#236"/></svg>')
    wait(page,"document.querySelector('.img-lightbox img').naturalWidth===600")
    s=snap(page)
    check(s['nw']==600 and s['nh']==1800 and abs(s['scale']-s['fit'])<1e-8 and s['alt']=='Delayed portrait','different dimensions reconcile to fit')
    # Reset after keyboard navigation during a captured mouse drag, too.
    page.mouse.move(cx,cy);page.mouse.down();page.keyboard.press('ArrowLeft')
    page.mouse.move(cx-60,cy);page.mouse.up();page.wait_for_timeout(60)
    check(abs(snap(page)['scale']-snap(page)['fit'])<1e-8 and not page.evaluate("document.querySelector('.img-lightbox')._imageViewer.dragging"),'navigation releases pointer capture')
    # Close while another replacement is pending; its eventual load is inert.
    page.keyboard.press('ArrowRight');page.wait_for_timeout(60)
    page.keyboard.press('Escape');wait(page,"!document.querySelector('.img-lightbox')")
    if pending:pending.pop().fulfill(status=200,content_type='image/svg+xml',body='<svg xmlns="http://www.w3.org/2000/svg" width="700" height="900"/>')
    page.unroute('**/zoom-delayed.svg')
    check(page.locator('.img-lightbox').count()==0,'close before load remains closed')
    page.evaluate(FIXTURE)


def mermaid_checks(page, engine):
    page.evaluate("""()=>{const h=document.createElement('div');h.id='mermaid-fixture';h.style.width='100%';
      h.innerHTML='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 600"><rect width="900" height="600" fill="#123456"/></svg>';
      document.querySelector('#msgInner').appendChild(h);_mountMermaidViewer(h.querySelector('svg'));}""")
    data=page.evaluate("""()=>{const s=document.querySelector('#mermaid-fixture .mermaid-viewer')._mermaidViewer;
      const labels=Array.from(s.toolbar.children,b=>b.getAttribute('aria-label'));s.zoomIn();const zoom=s.scale;s.reset();return {labels,zoom,reset:s.scale};}""")
    check(data['labels']==['Zoom in','Zoom out','Reset view','Fit to screen','Fullscreen'] and data['reset']==1,'inline Mermaid toolbar/reset retained')
    page.locator('#mermaid-fixture').get_by_role('button',name='Fullscreen',exact=True).click()
    s=page.evaluate("""()=>{const s=document.querySelector('.img-lightbox .mermaid-viewer')._mermaidViewer;
      const fit=s.fitScale;s.zoomIn();const zoom=s.scale; s.reset();return {fit,zoom,reset:s.scale,w:s.viewport.clientWidth,h:s.viewport.clientHeight};}""")
    check(s['zoom']>s['fit'] and s['reset']==1,'Mermaid lightbox retained')
    page.keyboard.press('Escape');wait(page,"!document.querySelector('.img-lightbox')")
    page.evaluate("document.querySelector('#mermaid-fixture').remove()")
    print(f'OK {engine}: inline and fullscreen Mermaid controls')


def resize_safe_area_checks(page):
    original=page.viewport_size
    open_image(page);fit(page)
    page.set_viewport_size({'width':820,'height':1180});page.wait_for_timeout(150)
    s=snap(page);check(abs(s['scale']-s['fit'])<1e-8,'fit retained after resize')
    wheel(page,s['vl']+s['vw']/2,s['vt']+s['vh']/2,delta=-650);old=snap(page)
    page.set_viewport_size({'width':844,'height':390});page.wait_for_timeout(150)
    s=snap(page);check(abs(s['scale']-old['scale'])<1e-8,'manual scale retained after resize')
    check(s['left']<=s['vl']+1 and s['left']+s['width']>=s['vl']+s['vw']-1,'resized horizontal bounds')
    # Simulate nonzero CSS environment values using the actual stylesheet.
    # This checks geometry, not a claim of hardware notch emulation on WebKit.
    page.evaluate("""async()=>{let css=await (await fetch('/static/style.css')).text();
      for(const [side,value] of Object.entries({top:44,bottom:34,left:24,right:24}))
        css=css.replaceAll('env(safe-area-inset-'+side+')',value+'px');
      const style=document.createElement('style');style.id='zoom-safe-area-probe';style.textContent=css;document.head.appendChild(style);}""")
    page.set_viewport_size({'width':390,'height':844});page.wait_for_timeout(150)
    geometry=page.evaluate("""()=>{
      const r=s=>document.querySelector(s).getBoundingClientRect().toJSON();
      const close=r('.img-lightbox-close'),toolbar=r('.img-zoom-toolbar'),v=r('.img-zoom-viewport');
      return {close,toolbar,v,buttons:Array.from(document.querySelectorAll('.img-zoom-toolbar button,.img-lightbox-nav'),b=>b.getBoundingClientRect().toJSON())};}""")
    check(geometry['close']['top']>=44 and geometry['close']['bottom']<=geometry['v']['top'],'safe-area close separated from surface')
    check(geometry['toolbar']['bottom']<=844-34 and geometry['toolbar']['left']>=24 and geometry['toolbar']['right']<=390-24,'safe-area toolbar contained')
    check(all(b['left']>=24 and b['right']<=390-24 for b in geometry['buttons']),'safe-area navigation/control sides')
    page.evaluate("document.querySelector('#zoom-safe-area-probe').remove()")
    page.keyboard.press('Escape');wait(page,"!document.querySelector('.img-lightbox')")
    page.set_viewport_size(original);page.wait_for_timeout(150)


def screenshots(page, directory, name, before):
    open_image(page)
    directory.mkdir(parents=True,exist_ok=True)
    facts={}
    if not before:facts['fit']=snap(page)
    page.screenshot(path=str(directory/f'{name}-fit.png'))
    if not before:
        # Six 1.2x button steps give approximately 3x fit consistently across
        # device scale factors (Chromium scales trusted wheel deltas on touch).
        for _ in range(6):page.get_by_role('button',name='Zoom in',exact=True).click()
        facts['zoom']=snap(page)
        page.screenshot(path=str(directory/f'{name}-zoom.png'))
        s=snap(page);page.mouse.move(s['vl']+s['vw']/2,s['vt']+s['vh']/2);page.mouse.down();page.mouse.move(-3000,-3000);page.mouse.up()
        facts['edge']=snap(page)
        page.screenshot(path=str(directory/f'{name}-edge.png'))
    (directory/f'{name}.json').write_text(json.dumps(facts,indent=2))
    page.keyboard.press('Escape');wait(page,"!document.querySelector('.img-lightbox')")


def run_browser(pw, engine, base, args):
    browser=getattr(pw,engine).launch(headless=True,**({'args':['--no-sandbox','--disable-dev-shm-usage']} if engine=='chromium' else {}))
    try:
        # Single context at a time, comfortably below the two-context memory cap.
        sizes=[('desktop',1440,900,False),('phone',390,844,True)]
        if engine=='chromium' and args.shots and not args.before:sizes.append(('tablet',820,1180,True))
        for name,w,h,mobile in sizes:
            context=browser.new_context(service_workers='block',viewport={'width':w,'height':h},is_mobile=mobile,has_touch=mobile,device_scale_factor=3 if mobile else 1)
            try:
                page=context.new_page()
                page._chromium = engine == 'chromium'
                page._mobile_webkit = engine == 'webkit' and mobile
                page.goto(base,wait_until='domcontentloaded');page.wait_for_selector('#msg',timeout=15000)
                wait(page,"typeof _openImgLightbox === 'function'")
                page.evaluate(FIXTURE)
                if not args.before:
                    zoom_checks(page,browser,engine)
                    mermaid_checks(page,engine)
                    resize_safe_area_checks(page)
                    if mobile:
                        open_image(page)
                        rects=page.locator('.img-zoom-toolbar button, .img-lightbox-close, .img-lightbox-nav').evaluate_all('(bs)=>bs.map(b=>({w:b.getBoundingClientRect().width,h:b.getBoundingClientRect().height}))')
                        check(all(r['w']>=44 and r['h']>=44 for r in rects),'touch targets >=44px')
                        page.keyboard.press('Escape');wait(page,"!document.querySelector('.img-lightbox')")
                if args.shots:
                    screenshots(page,Path(args.shots),f'{"before-" if args.before else ""}{engine}-{name}',args.before)
            finally:
                context.close()
    finally:
        browser.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',type=Path,default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--engine',choices=['all','chromium','webkit'],default='all')
    parser.add_argument('--shots')
    parser.add_argument('--before',action='store_true')
    args=parser.parse_args()
    from playwright.sync_api import sync_playwright
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    state=Path(tempfile.mkdtemp(prefix='image-zoom-'))
    (state/'settings.json').write_text(json.dumps({'check_for_updates':False}))
    env=os.environ.copy()
    for key in list(env):
        if key.endswith('_API_KEY') or key.startswith('HERMES_WEBUI_TLS_'):env.pop(key,None)
    env.update(HOME=str(state), HERMES_REAL_HOME=str(state), HERMES_DISABLE_LAZY_INSTALLS='1',
               HERMES_WEBUI_PORT=str(port),HERMES_WEBUI_HOST='127.0.0.1',HERMES_WEBUI_STATE_DIR=str(state),
               HERMES_HOME=str(state),HERMES_BASE_HOME=str(state),HERMES_WEBUI_SKIP_ONBOARDING='1',
               HERMES_WEBUI_AGENT_DIR=str(state/'no-agent'),HERMES_WEBUI_NO_DOTENV='1')
    log=(state/'server.log').open('w')
    proc=subprocess.Popen([sys.executable,str(args.repo/'server.py')],cwd=args.repo,env=env,stdout=log,stderr=subprocess.STDOUT)
    try:
        base=f'http://127.0.0.1:{port}'
        deadline=time.monotonic()+30
        while True:
            try:
                with urllib.request.urlopen(base+'/health',timeout=1) as response:
                    if response.status==200:break
            except OSError:pass
            if time.monotonic()>deadline:raise RuntimeError('server boot failed: '+(state/'server.log').read_text()[-2000:])
            time.sleep(.2)
        with sync_playwright() as pw:
            for engine in ['chromium','webkit'] if args.engine=='all' else [args.engine]:run_browser(pw,engine,base,args)
        print('IMAGE LIGHTBOX ZOOM PASSED' if not args.before else 'BEFORE SCREENSHOTS CAPTURED')
        return 0
    except AssertionError as exc:
        print('IMAGE LIGHTBOX ZOOM FAILED: '+str(exc),file=sys.stderr);return 1
    finally:
        proc.terminate()
        try:proc.wait(timeout=5)
        except subprocess.TimeoutExpired:proc.kill();proc.wait()
        log.close()
        # Remove only this gate's named files; leave isolated state folders to
        # the supervisor when the server generated additional state inside them.
        print('Isolated gate state: '+str(state))


if __name__=='__main__':sys.exit(main())
