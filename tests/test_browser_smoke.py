import functools
import http.server
import sys
import threading
from pathlib import Path

import pytest

import tweet_cleanup as app

pytestmark = pytest.mark.usefixtures('data_dir')


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows default browser required')
def test_default_browser_smoke(tmp_path, playwright):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            self.path = '/index.html'
            super().do_GET()

    handler = functools.partial(Handler, directory=str(Path(__file__).parent / 'fixtures'))
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        run = app.launch_browser(playwright)
        try:
            run.page.goto(f'http://127.0.0.1:{server.server_port}/index.html')
            results = app.clean(run.page, {'handle': 'me', 'reposts': {}}, tmp_path / 'state.json',
                                timing=app.Timing(settle_ms=60))
            assert results
            assert run.page.evaluate('window.removed') == []
        finally:
            run.close()
        assert not app.profile_open(tmp_path / 'browser-profile')
    finally:
        server.shutdown()
        server.server_close()
