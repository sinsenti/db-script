#!/usr/bin/env python3
"""Real TUI + browser diagram checks. SQLite fixtures are disposable.

Optional --postgres-container reads metadata from that explicit container only.
Needs pyte and Playwright; --firefox also needs Selenium and a geckodriver.
The fake desktop opener captures the real file argument without opening a user tab.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'benchmarks/quotes_perf'))
from terminal import Terminal


def document(path):
    match = re.search(r'<script type="application/json" id="schema-data">(.*?)</script>', path.read_text(), re.S)
    assert match, 'Diagram data absent'
    return json.loads(match[1])


def tui(folder, database=None, container=None):
    opener = folder / 'xdg-open'
    capture = folder / 'opened.json'
    opener.write_text('#!/usr/bin/env python3\nimport json,os,sys\nfrom pathlib import Path\n'
                      f'Path({str(capture)!r}).write_text(json.dumps(dict(path=sys.argv[1],credentials=any(k.startswith("DBCLI_") or k=="PGPASSWORD" for k in os.environ))))\n')
    opener.chmod(0o700)
    env = dict(os.environ, TERM='xterm-256color', PATH=str(folder)+os.pathsep+os.environ['PATH'],
               DBCLI_DIAGRAM_TEST_CREDENTIAL='test-only')
    command = [str(ROOT/'ui/rust/target/release/dbcli-ui-rust'), '--backend', 'original-rust',
               '--worker', str(ROOT/'rust/target/release/dbcli-rust'), '--read-only']
    if container:
        from dbcli.postgres_target import docker_target, target_environment
        url, _ = docker_target(container, read_only=True)
        env.update(target_environment(url, read_only=True))
        command += ['--engine', 'postgres', '--table', 'public.quotes']
    else:
        command += ['--engine', 'sqlite', '--database', str(database), '--table', 'child']
    # target_environment returns a full environment, so install the test opener
    # after resolving PostgreSQL credentials as well as on the SQLite path.
    env.update(TERM='xterm-256color',PATH=str(folder)+os.pathsep+os.environ['PATH'],DBCLI_DIAGRAM_TEST_CREDENTIAL='test-only')
    t = Terminal(command, ROOT, env, rows=44, columns=180)
    paths = []
    try:
        assert t.wait(lambda:'Tables focused' in t.display and 'BUSY' not in t.display and ' rows ·' in t.display), 'Initial load failed'
        t.send('h'); assert t.process.poll() is None and 'Tables focused' in t.display
        for pane in ['sidebar','sidebar','rows']:
            if pane=='rows':t.send('\t')
            capture.unlink(missing_ok=True)
            t.send('D')
            assert t.wait(lambda:capture.is_file() and 'Database schema diagram' in t.display), 'D did not create/open a diagram: ' + ' '.join(line.strip() for line in t.display.splitlines() if any(word in line for word in ['Error','failed','exceeds','rejected','Invalid','SQLSTATE']))
            opened = json.loads(capture.read_text())
            assert not opened['credentials'], 'Desktop opener inherited DB credentials'
            path = Path(opened['path'])
            assert path.is_file() and path.parent.parent == Path.home()/'dbcli-diagrams'
            assert path.stat().st_mode & 0o777 == 0o600
            assert path.parent.stat().st_mode & 0o777 == 0o700
            paths.append(path)
            t.send('h'); assert 'Database schema diagram' in t.display, 'h closed diagram modal'
            t.send('q'); assert 'Database schema diagram' not in t.display
        t.send('h'); assert t.process.poll() is None and 'Rows focused' in t.display
        t.send('\r'); assert t.wait(lambda:'Row detail' in t.display)
        t.send('h'); assert 'Row detail' in t.display, 'h closed row details'
        t.send('q')
        t.send('?'); t.send('h'); assert 'SEARCH & QUERY' in t.display, 'h closed help'
        t.send('q')
    finally:
        life = t.close()
    assert life == dict(exit_code=0, forced_exit=False, termios_restored=True, alternate_screen_restored=True), life
    assert paths[0] != paths[1] and all(p.is_file() for p in paths), 'Snapshot removed on regeneration or exit'
    return paths


def chrome_check(path, executable, folder):
    from playwright.sync_api import sync_playwright
    model = document(path)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=executable, headless=True)
        context = browser.new_context(viewport=dict(width=1600,height=1000),accept_downloads=True)
        page = context.new_page()
        errors, network = [], []
        page.on('pageerror', lambda e:errors.append(str(e)))
        page.on('request', lambda r:network.append(r.url) if r.url.startswith(('http:','https:')) else None)
        page.goto(path.as_uri())
        page.wait_for_function('!document.getElementById("summary").textContent.includes("Loading")')
        assert page.locator('#tables > g').count() == len(model['tables'])
        assert page.locator('#edges > g').count() == len(model['foreign_keys'])
        assert page.evaluate('window.__dbcliInjected') is None
        page.reload(); page.wait_for_function('document.querySelectorAll("#tables > g").length>0')
        node = next((t for t in model['tables'] if t['name']=='node'),model['tables'][0])
        page.locator('#search').fill(node['name'])
        page.locator('#table-list button').first.click()
        page.locator('#mode').select_option('neighbours')
        count = page.locator('#tables > g').count()
        assert 0 < count <= len(model['tables'])
        page.locator('#mode').select_option('all'); page.locator('#search').fill('')
        all_columns = page.locator('#tables .column-name').count()
        page.locator('#keys-only').check()
        assert page.locator('#tables .column-name').count() <= all_columns
        page.locator('#keys-only').uncheck()
        if model['foreign_keys']:
            edge = model['foreign_keys'][0]
            page.locator(f'#edges [data-edge="{edge["id"]}"]').focus()
            page.keyboard.press('Enter')
            assert edge['pairs'][0][0] in page.locator('#details').inner_text()
        old_zoom = page.locator('#zoom-label').inner_text()
        page.locator('#zoom-in').click()
        assert page.locator('#zoom-label').inner_text()!=old_zoom
        page.locator('#fit').click()
        card = page.locator('#tables > g').first
        old_transform = card.get_attribute('transform')
        box = card.bounding_box()
        page.mouse.move(box['x']+30,box['y']+20);page.mouse.down()
        page.mouse.move(box['x']+75,box['y']+45,steps=4);page.mouse.up()
        assert card.get_attribute('transform')!=old_transform
        for button, name in [('svg-export','schema.svg'),('mermaid-export','schema.mmd'),('html-export','schema.html')]:
            with page.expect_download() as event:
                page.locator('#'+button).click()
            output = folder/name;event.value.save_as(output)
            assert output.stat().st_size>0
        assert ET.parse(folder/'schema.svg').getroot().tag.endswith('svg')
        assert (folder/'schema.mmd').read_text().startswith('erDiagram\n')
        page.goto((folder/'schema.html').as_uri())
        page.wait_for_function('document.querySelectorAll("#tables > g").length>0')
        assert page.locator('#tables > g').count()==len(model['tables'])
        assert page.locator('#schema option').count()==len(set(t['schema'] for t in model['tables']))+1
        page.screenshot(path=str(ROOT/'.local/diagram-acceptance.png'),full_page=True)
        assert not errors, errors
        assert not network, 'Offline diagram made network requests'
        browser.close()
    return dict(tables=len(model['tables']),foreign_keys=len(model['foreign_keys']),reload=True,exports=True,offline=True)


def legacy_h_check(database):
    terminal = Terminal([str(ROOT/'rust/target/release/dbcli-rust'),'--db',str(database),'--table','child','--read-only'],
                        ROOT,dict(os.environ,TERM='xterm-256color'),columns=160)
    try:
        assert terminal.wait(lambda:'Vim Nav:' in terminal.display), 'Native browser failed to load'
        terminal.send('h');assert terminal.process.poll() is None and 'Vim Nav:' in terminal.display
        terminal.send('\r');assert terminal.wait(lambda:'ROW #1 DETAIL VIEW' in terminal.display)
        terminal.send('h');assert 'ROW #1 DETAIL VIEW' in terminal.display
        terminal.send('q');assert terminal.wait(lambda:'Vim Nav:' in terminal.display)
    finally:
        lifecycle=terminal.close()
    assert lifecycle['exit_code']==0 and not lifecycle['forced_exit'] and lifecycle['termios_restored']


def firefox_check(path):
    from selenium import webdriver
    from selenium.webdriver.firefox.options import Options
    from selenium.webdriver.firefox.service import Service
    from selenium.webdriver.support.ui import WebDriverWait
    options = Options();options.add_argument('-headless')
    options.binary_location = '/snap/firefox/current/usr/lib/firefox/firefox'
    service = Service(executable_path=shutil.which('geckodriver'),log_output=os.devnull)
    driver = webdriver.Firefox(options=options,service=service)
    try:
        driver.set_page_load_timeout(20)
        driver.get(path.as_uri())
        WebDriverWait(driver,15).until(lambda d:d.execute_script('return document.querySelectorAll("#tables > g").length')>0)
        count=driver.execute_script('return document.querySelectorAll("#tables > g").length')
        driver.refresh()
        assert driver.execute_script('return document.querySelectorAll("#tables > g").length')==count
        return dict(snap_firefox=True,tables=count,reload=True)
    finally:
        driver.quit()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser',default=shutil.which('google-chrome'))
    parser.add_argument('--firefox',action='store_true')
    parser.add_argument('--postgres-container')
    args=parser.parse_args()
    assert args.browser, 'Supply --browser PATH to a Chromium-compatible executable'
    generated=[]
    try:
        with tempfile.TemporaryDirectory(prefix='dbcli-diagram-test-') as temp:
            folder=Path(temp);database=folder/'fixture.sqlite'
            with sqlite3.connect(database) as c:
                c.executescript('''CREATE TABLE parent(a TEXT NOT NULL,b INTEGER NOT NULL,name TEXT,PRIMARY KEY(a,b));
                CREATE TABLE child(id INTEGER PRIMARY KEY,parent_a TEXT NOT NULL,parent_b INTEGER NOT NULL,tag TEXT,UNIQUE(parent_a,parent_b),FOREIGN KEY(parent_a,parent_b) REFERENCES parent(a,b) ON DELETE CASCADE);
                CREATE TABLE node(id INTEGER PRIMARY KEY,parent_id INTEGER REFERENCES node(id));
                CREATE TABLE unlinked(id INTEGER PRIMARY KEY);
                CREATE TABLE "odd </script><script>window.__dbcliInjected=1</script>"("a.b" TEXT);
                CREATE VIEW child_names AS SELECT id,tag FROM child;
                INSERT INTO parent VALUES('p',1,'fixture');INSERT INTO child VALUES(1,'p',1,'fixture');INSERT INTO node VALUES(1,NULL);''')
            generated.extend(tui(folder,database=database))
            model=document(generated[-1]);assert len(model['tables'])==6 and len(model['foreign_keys'])==2
            composite=next(f for f in model['foreign_keys'] if len(f['pairs'])==2)
            assert composite['pairs']==[['parent_a','a'],['parent_b','b']] and composite['unique'] and not composite['optional']
            legacy_h_check(database)
            result={'sqlite':chrome_check(generated[-1],args.browser,folder),'persistence_after_exit':True,'h_is_not_back':True}
            if args.firefox:result['firefox']=firefox_check(generated[-1])
            if args.postgres_container:
                generated.extend(tui(folder,container=args.postgres_container))
                result['postgres']=chrome_check(generated[-1],args.browser,folder)
            print(json.dumps(result,indent=2))
    finally:
        # Remove only exact snapshot folders produced by this test invocation.
        for path in generated:
            if path.parent.parent==Path.home()/'dbcli-diagrams':shutil.rmtree(path.parent)


if __name__=='__main__':main()
