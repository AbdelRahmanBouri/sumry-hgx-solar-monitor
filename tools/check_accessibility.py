#!/usr/bin/env python3
"""Check the six demo views with pinned axe-core and real keyboard events.

Run npm ci --ignore-scripts first. The script only visits the configured local
simulator; a clean result is not a claim of full WCAG conformance.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import urllib.request

from screenshot import READY, VIEWS, Browser, find_chromium, page_errors, session_cookie


def press(browser, session, key):
    code = {'Tab': 9, 'Enter': 13, 'Escape': 27, 'ArrowRight': 39, ' ': 32}[key]
    for kind in ('keyDown', 'keyUp'):
        browser.send('Input.dispatchKeyEvent', {'type': kind, 'key': key,
                     'windowsVirtualKeyCode': code, 'code': 'Space' if key == ' ' else key,
                     'text': ('\r' if key == 'Enter' else ' ' if key == ' ' else '') if kind == 'keyDown' else ''}, session)


def tab_to(browser, session, selector):
    # Restart sequential navigation at the document, then use only Tab.
    browser.evaluate("document.activeElement.blur(); document.body.tabIndex=-1; document.body.focus()", session)
    for _ in range(160):
        press(browser, session, 'Tab')
        if browser.evaluate(f'document.activeElement.matches({json.dumps(selector)})', session):
            focus = browser.evaluate("getComputedStyle(document.activeElement).outlineStyle", session)
            if focus == 'none':
                raise AssertionError(f'No visible keyboard outline: {selector}')
            return
    raise AssertionError(f'Keyboard could not reach {selector}')


def axe_report(browser, session):
    return browser.evaluate("axe.run(document, {runOnly: {type: 'tag', values: "
                            "['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'best-practice']}})", session)


def failed(report):
    return bool(report['violations'] or report['keyboard_errors'] or report['page_errors']
                or any(state['violations'] for state in report.get('states', [])))


def verify_engine(browser, axe):
    target = browser.send('Target.createTarget', {'url': 'about:blank'})['targetId']
    session = browser.send('Target.attachToTarget', {'targetId': target, 'flatten': True})['sessionId']
    browser.evaluate("document.body.innerHTML = '<button></button><p style=\"color:#ccc;background:#fff\">Low contrast fixture</p>'", session)
    browser.evaluate(axe, session)
    violations = {item['id'] for item in axe_report(browser, session)['violations']}
    if not {'button-name', 'color-contrast'} <= violations:
        raise AssertionError(f'Accessibility engine missed deliberately broken content: {violations}')
    browser.send('Target.closeTarget', {'targetId': target})


def keyboard_check(browser, session, name, report):
    tab_to(browser, session, '.skip-link')
    press(browser, session, 'Enter')
    if not browser.evaluate("document.activeElement.id === 'main-content'", session):
        raise AssertionError('Skip link did not move focus to main content')
    if name.startswith('dashboard'):
        tab_to(browser, session, '#customize-open')
        press(browser, session, 'Enter')
        browser.wait_until("document.querySelector('#customize-dialog').open", session)
        report['states'].append(axe_report(browser, session))
        tab_to(browser, session, '.card-control:first-child button:not(:disabled)')
        moved = browser.evaluate("document.activeElement.closest('.card-control').querySelector('span').textContent", session)
        press(browser, session, 'Enter')
        focused = browser.evaluate("document.activeElement.closest('.card-control')?.querySelector('span').textContent", session)
        if focused != moved:
            raise AssertionError('Moving a card lost keyboard focus')
        for _ in range(12):
            press(browser, session, 'Tab')
            if not browser.evaluate("document.activeElement.closest('#customize-dialog') !== null", session):
                raise AssertionError('Tab escaped the modal dialog')
        press(browser, session, 'Escape')
        browser.wait_until("!document.querySelector('#customize-dialog').open", session)
        if not browser.evaluate("document.activeElement.id === 'customize-open'", session):
            raise AssertionError('Closing the dialog did not restore focus')
        tab_to(browser, session, '[data-hours="168"]')
        press(browser, session, 'Enter')
        browser.wait_until('selectedHours === 168 && dailyRows.length > 0', session)
        tab_to(browser, session, '#chart-data summary')
        press(browser, session, 'Enter')
        browser.wait_until("document.querySelector('#chart-data').open", session)
        report['states'].append(axe_report(browser, session))
    elif name.startswith('estimate'):
        tab_to(browser, session, '[data-hours="6"]')
        press(browser, session, 'Enter')
        browser.wait_until("document.querySelector('[data-hours=\"6\"]').classList.contains('active')", session)
        tab_to(browser, session, '#potential-chart')
        press(browser, session, 'ArrowRight')
        browser.wait_until("!document.querySelector('#potential-reading').hidden", session)
        tab_to(browser, session, '#solar-data summary')
        press(browser, session, 'Enter')
        browser.wait_until("document.querySelector('#solar-data').open && document.querySelector('#potential-data tr') !== null", session)
        report['states'].append(axe_report(browser, session))
    else:
        tab_to(browser, session, '[data-view="inverter"]')
        press(browser, session, 'Enter')
        browser.wait_until(READY[name], session)


def check(base, out, axe_path):
    with urllib.request.urlopen(base + '/api/status', timeout=30) as response:
        status = json.load(response)
    if status.get('collector', {}).get('configured_port') != 'sim://':
        raise RuntimeError('Accessibility checks require the sim:// demo')
    binary = find_chromium()
    if not binary:
        raise RuntimeError('No Chromium found')
    axe = axe_path.read_text()
    cookie = session_cookie(base, 'demo:demo-password')
    browser = Browser(binary)
    reports = {}
    out.mkdir(parents=True, exist_ok=True)
    try:
        verify_engine(browser, axe)
        for name, (path, width, height, scheme, _, before) in VIEWS.items():
            browser.events.clear()
            target = browser.send('Target.createTarget', {'url': 'about:blank'})['targetId']
            session = browser.send('Target.attachToTarget', {'targetId': target, 'flatten': True})['sessionId']
            browser.send('Emulation.setDeviceMetricsOverride', {
                'width': width, 'height': height, 'deviceScaleFactor': 1, 'mobile': width < 700}, session)
            browser.send('Emulation.setEmulatedMedia', {'features': [
                {'name': 'prefers-color-scheme', 'value': scheme}]}, session)
            for domain in ('Network', 'Runtime', 'Log', 'Page'):
                browser.send(domain + '.enable', {}, session)
            browser.send('Network.setCookie', {'name': 'session', 'value': cookie, 'url': base,
                         'path': '/', 'httpOnly': True}, session)
            browser.send('Page.navigate', {'url': base + path}, session)
            browser.wait_for('Page.loadEventFired', session=session)
            if before:
                browser.evaluate(before, session)
            browser.wait_until(READY[name], session)
            browser.evaluate(axe, session)
            report = axe_report(browser, session)
            report['states'] = []
            report['keyboard_errors'] = []
            try:
                keyboard_check(browser, session, name, report)
            except (AssertionError, RuntimeError, TimeoutError) as exc:
                report['keyboard_errors'].append(str(exc))
            report['page_errors'] = page_errors(browser.events, session)
            (out / f'{name}.json').write_text(json.dumps(report, indent=2))
            reports[name] = report
            print(name, 'violations:', sum(len(r['violations']) for r in [report, *report['states']]), 'keyboard:', report['keyboard_errors'],
                  'page:', report['page_errors'])
            for violation in [v for r in [report, *report['states']] for v in r['violations']]:
                print(' ', violation['id'], [node['target'] for node in violation['nodes']])
            browser.send('Target.closeTarget', {'targetId': target})
    finally:
        browser.close()
    return int(any(failed(r) for r in reports.values()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='http://127.0.0.1:5000')
    parser.add_argument('--out', type=Path, default=Path('accessibility-reports'))
    parser.add_argument('--axe', type=Path, default=Path('node_modules/axe-core/axe.min.js'))
    args = parser.parse_args()
    return check(args.base.rstrip('/'), args.out, args.axe)


if __name__ == '__main__':
    sys.exit(main())
