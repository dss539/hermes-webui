"""Review regressions: dispatch through registered DOM listeners, no replacements."""
import pytest
from tests._pytest_port import BASE

@pytest.fixture(params=['chromium', 'webkit'])
def page(request):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = getattr(p, request.param).launch(headless=True)
        context = browser.new_context(viewport={'width':1000, 'height':800}, has_touch=True)
        page = context.new_page()
        page.goto(BASE, wait_until='domcontentloaded')
        for _ in range(200):
            if page.evaluate("typeof _openImgLightboxWithNav === 'function'"):
                break
            page.wait_for_timeout(25)
        page.evaluate("""() => {
          const src='data:image/svg+xml,'+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="2000" height="1200"><rect width="2000" height="1200" fill="#378"/></svg>');
          _openImgLightboxWithNav(src,'ownership review',[{src},{src}],0);
          window.lb=document.querySelector('.img-lightbox');
          window.vp=lb.querySelector('.img-lightbox-viewport');
          window.z=lb._zoom;
        }""")
        for _ in range(200):
            if page.evaluate('z.boxW === 2000'):
                break
            page.wait_for_timeout(25)
        assert page.evaluate('z.boxW === 2000')
        yield page
        context.close()
        browser.close()


def test_foreign_cancel_preserves_mouse_pan(page):
    page.evaluate('z.zoomBy(2)')
    page.mouse.move(500,400)
    page.mouse.down()
    page.mouse.move(520,400)
    result=page.evaluate("""() => {
      const before={dragging:z.dragging, owner:z.dragPointerId};
      vp.dispatchEvent(new PointerEvent('pointerdown',{
        pointerType:'touch',pointerId:999,clientX:400,clientY:400,bubbles:true,cancelable:true
      }));
      const touchStart=new Event('touchstart',{bubbles:true,cancelable:true});
      Object.defineProperty(touchStart,'touches',{value:[{identifier:999,target:vp,clientX:400,clientY:400}]});
      vp.dispatchEvent(touchStart);
      vp.dispatchEvent(new PointerEvent('pointercancel',{
        pointerType:'touch',pointerId:999,clientX:400,clientY:400,bubbles:true,cancelable:true
      }));
      return {before, after:{dragging:z.dragging,owner:z.dragPointerId}};
    }""")
    page.mouse.up()
    assert result['before']['dragging'] is True
    assert result['after'] == result['before'], result


def test_refused_touch_cannot_navigate_while_mouse_owns_pan(page):
    page.mouse.move(500,400)
    page.mouse.down()
    result=page.evaluate("""() => {
      const owner=z.dragPointerId;
      for(const [type,x] of [['pointerdown',600],['pointermove',400],['pointerup',400]]){
        vp.dispatchEvent(new PointerEvent(type,{
          pointerType:'touch',pointerId:999,clientX:x,clientY:400,bubbles:true,cancelable:true
        }));
        const tt=type==='pointerdown'?'touchstart':type==='pointerup'?'touchend':'touchmove';
        const te=new Event(tt,{bubbles:true,cancelable:true});
        Object.defineProperty(te,'touches',{value:type==='pointerup'?[]:[{identifier:999,target:vp,clientX:x,clientY:400}]});
        vp.dispatchEvent(te);
      }
      return {index:lb._navIndex, owner, afterOwner:z.dragPointerId, dragging:z.dragging};
    }""")
    page.mouse.up()
    assert result['index'] == 0, result
    assert result['dragging'] and result['owner'] == result['afterOwner'], result
