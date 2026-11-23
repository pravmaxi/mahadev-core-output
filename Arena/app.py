"""Arena: two isolated paper engines behind one web server. No broker orders."""
import logging
import os
import re
import threading
from flask import Flask, redirect


class QuietPollingRequests(logging.Filter):
    """Hide only successful routine polling access lines, never errors or other logs."""
    _poll = re.compile(r'"(?:GET /(?:paper|live)/api/paper-live/status(?:\?[^ ]*)?|POST /(?:paper|live)/api/paper-live/ltp) HTTP/[^" ]+" 200(?:\s|$)')

    def filter(self, record):
        if record.levelno >= logging.WARNING:
            return True
        return not bool(self._poll.search(record.getMessage()))


if os.getenv('ARENA_VERBOSE_REQUESTS') != '1':
    logging.getLogger('werkzeug').addFilter(QuietPollingRequests())
import paper_engine
import live_engine

app = Flask(__name__)
app.register_blueprint(paper_engine.paper_live_bp, url_prefix="/paper", name="arena_paper")
app.register_blueprint(live_engine.paper_live_bp, url_prefix="/live", name="arena_live")
# yfinance is not thread-safe, including across our two independently loaded engines.
shared_data_lock = threading.Lock()
paper_engine.DATA_LOCK = live_engine.DATA_LOCK = shared_data_lock

@app.route('/')
def index():
    return redirect('/paper/paper-live')

# Start once per process, including when served through a WSGI server.
live_engine._sheet_start()

if __name__ == '__main__':
    port = int(os.environ.get('ARENA_PORT', '8020'))
    print('Arena: http://localhost:%s/' % port, flush=True)
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True, use_reloader=False)