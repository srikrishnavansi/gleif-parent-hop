PY ?= python3
CHROME ?= /Applications/Google Chrome.app/Contents/MacOS/Google Chrome

.PHONY: demo live test record screenshot

demo:            ## serve the screen on a free port, recorded GLEIF data, no network
	$(PY) app.py --open

live:            ## same screen, straight from api.gleif.org
	$(PY) app.py --live --open

test: .venv      ## pytest on the fixtures, no network
	.venv/bin/python -m pytest -q

.venv:
	$(PY) -m venv .venv && .venv/bin/pip install -q -r requirements-dev.txt

record:          ## re-record the GLEIF fixtures for the five demo companies
	$(PY) hop.py record

screenshot:      ## docs/screen.png with headless Chrome
	@PORT=$$($(PY) -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])'); \
	$(PY) app.py --port $$PORT & PID=$$!; sleep 1; \
	"$(CHROME)" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=2 --window-size=1440,1130 \
		--virtual-time-budget=4000 --screenshot=docs/screen.png "http://127.0.0.1:$$PORT/#06649982" 2>/dev/null; \
	kill $$PID
