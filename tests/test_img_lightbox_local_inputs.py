"""Distinct local input regressions on the upstream image-only zoom engine."""
from pathlib import Path

import pytest
from tests._pytest_port import BASE


@pytest.fixture(scope='module', params=['chromium', 'webkit'])
def browser(request):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        engine = getattr(p, request.param)
        b = engine.launch(headless=True, args=['--no-sandbox'] if request.param == 'chromium' else [])
        yield b
        b.close()


def _wait(page, expression):
    import time
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        # WebKit's wait_for_function helper uses eval, blocked by the real CSP.
        # Direct inspector evaluation does not require weakening that policy.
        if page.evaluate('() => (' + expression + ')'):
            return
        page.wait_for_timeout(25)
    raise AssertionError('Timed out waiting for ' + expression)


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={'width': 1000, 'height': 800}, has_touch=True)
    p = ctx.new_page()
    p.goto(BASE, wait_until='domcontentloaded')
    _wait(p, "typeof _openImgLightboxWithNav === 'function'")
    p.evaluate("""() => {
      const src = 'data:image/svg+xml,' + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="2000" height="1200"><rect width="2000" height="1200" fill="#378"/></svg>');
      _openImgLightboxWithNav(src, 'Synthetic input check', [{src}, {src}], 0);
      window.lb = document.querySelector('.img-lightbox');
      window.vp = lb.querySelector('.img-lightbox-viewport');
      window.z = lb._zoom;
    }""")
    _wait(p, 'z.boxW === 2000')
    yield p
    ctx.close()


def test_mouse_double_click_toggles_fit(page):
    page.mouse.dblclick(500, 400)
    assert page.evaluate('z.scale > z.fitScale')
    page.mouse.dblclick(500, 400)
    assert page.evaluate('Math.abs(z.scale - z.fitScale) < 1e-9')


def test_touch_double_tap_toggles_without_dismissal(page):
    page.touchscreen.tap(500, 400)
    page.touchscreen.tap(500, 400)
    assert page.evaluate('z.scale > z.fitScale && lb.isConnected')
    page.wait_for_timeout(320)
    page.touchscreen.tap(500, 400)
    page.touchscreen.tap(500, 400)
    assert page.evaluate('Math.abs(z.scale - z.fitScale) < 1e-9 && lb.isConnected')


def test_fit_swipe_navigates_but_zoomed_swipe_pans(page):
    result = page.evaluate("""() => {
      function swipe() {
        vp.dispatchEvent(new PointerEvent('pointerdown', {pointerType:'touch',pointerId:91,clientX:600,clientY:400,bubbles:true}));
        vp.dispatchEvent(new PointerEvent('pointermove', {pointerType:'touch',pointerId:91,clientX:400,clientY:400,bubbles:true}));
        vp.dispatchEvent(new PointerEvent('pointerup', {pointerType:'touch',pointerId:91,clientX:400,clientY:400,bubbles:true}));
      }
      swipe(); return lb._navIndex;
    }""")
    assert result == 1
    page.wait_for_timeout(50)
    page.evaluate('z.zoomBy(2)')
    page.evaluate("""() => {
      vp.dispatchEvent(new PointerEvent('pointerdown', {pointerType:'touch',pointerId:92,clientX:400,clientY:400,bubbles:true}));
      vp.dispatchEvent(new PointerEvent('pointermove', {pointerType:'touch',pointerId:92,clientX:600,clientY:400,bubbles:true}));
      vp.dispatchEvent(new PointerEvent('pointerup', {pointerType:'touch',pointerId:92,clientX:600,clientY:400,bubbles:true}));
    }""")
    assert page.evaluate('lb._navIndex') == 1


def test_safari_absolute_gestures_and_cleanup(page):
    result = page.evaluate("""() => {
      const fire = (type, scale) => { const e = new Event(type,{bubbles:true,cancelable:true}); Object.assign(e,{scale,clientX:500,clientY:400}); lb.dispatchEvent(e); return e.defaultPrevented; };
      window.fireGesture = fire;
      const start = z.scale;
      const prevented = fire('gesturestart',1);
      fire('gesturechange',1.5); fire('gesturechange',2);
      const doubled = Math.abs(z.scale - 2*start) < 1e-9;
      fire('gestureend',2);
      const end = z.scale;
      fire('gesturechange',3);
      return {prevented,doubled,noLateChange:z.scale===end};
    }""")
    assert result == {'prevented': True, 'doubled': True, 'noLateChange': True}
    page.evaluate('_closeImgLightbox(lb)')
    assert page.evaluate("""() => {const old=z.scale; fireGesture('gesturestart',1);fireGesture('gesturechange',2);return old===z.scale;}""")


def test_safari_does_not_double_apply_touch_pinch(page):
    result = page.evaluate("""() => {
      const touches=[{identifier:1,target:vp,clientX:400,clientY:400},{identifier:2,target:vp,clientX:600,clientY:400}];
      // Desktop WebKit has no constructible Touch; dispatch the same contact
      // data through the real listeners without replacing any app handler.
      const touchStart=new Event('touchstart',{bubbles:true,cancelable:true});
      Object.defineProperty(touchStart,'touches',{value:touches});
      vp.dispatchEvent(touchStart);
      const before=z.scale;
      for(const [type,scale] of [['gesturestart',1],['gesturechange',2]]) {
        const e=new Event(type,{bubbles:true,cancelable:true});Object.assign(e,{scale});lb.dispatchEvent(e);
      }
      return before===z.scale && z.pinching;
    }""")
    assert result


def test_backdrop_wheel_is_owned_by_modal(page):
    result = page.evaluate("""() => {
      const before=z.scale;
      const e=new WheelEvent('wheel',{deltaY:-150,clientX:500,clientY:400,bubbles:true,cancelable:true});lb.dispatchEvent(e);
      return {prevented:e.defaultPrevented,zoomed:z.scale>before};
    }""")
    assert result == {'prevented': True, 'zoomed': True}


def test_hybrid_touch_targets_have_explicit_any_pointer_rule():
    # Installed Chromium cannot emulate any-pointer independently of pointer.
    # Source guard checks the actual shipped CSS; no CSS mutation in a browser.
    css = (Path(__file__).resolve().parents[1] / 'static/style.css').read_text()
    assert '@media (pointer:fine) and (any-pointer:coarse)' in css


def test_navigation_preserves_selected_zoom(page):
    # Navigation preserves deliberate zoom instead of the old local reset policy.
    page.evaluate('z.zoomBy(2); window.keptScale=z.scale; _navigateLightbox(lb,1)')
    _wait(page, '!z.pendingNav')
    assert page.evaluate('Math.abs(z.scale-keptScale)<1e-9')


def test_drag_returning_to_tap_origin_does_not_complete_double_tap(page):
    page.touchscreen.tap(500, 400)
    page.evaluate("""() => {
      for(const [type,x] of [['pointerdown',500],['pointermove',550],['pointermove',500],['pointerup',500]]){
        vp.dispatchEvent(new PointerEvent(type,{pointerType:'touch',pointerId:99,clientX:x,clientY:400,bubbles:true}));
      }
    }""")
    assert page.evaluate('Math.abs(z.scale-z.fitScale)<1e-9')
