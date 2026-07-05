.PHONY: install install-ml ingest ingest-hh ingest-tg ingest-cbr publish publish-events publish-weekly test test-slow lint dryrun clean

PYTHON ?= D:/Python/Python312/python.exe

install:
	$(PYTHON) -m pip install --timeout 300 -e ".[dev,reports]"

install-ml:
	$(PYTHON) -m pip install --timeout 600 -e ".[ml]"
	$(PYTHON) -m spacy download ru_core_news_sm

ingest: ingest-cbr ingest-hh ingest-tg

ingest-hh:
	$(PYTHON) -m src.cli ingest hh

ingest-tg:
	$(PYTHON) -m src.cli ingest telegram

ingest-cbr:
	$(PYTHON) -m src.cli ingest cbr

publish:
	$(PYTHON) -m src.cli publish slim

publish-events:
	$(PYTHON) -m src.cli publish events

publish-weekly:
	$(PYTHON) -m src.cli publish weekly

test:
	$(PYTHON) -m pytest -q

test-slow:
	$(PYTHON) -m pytest -q -m slow

lint:
	$(PYTHON) -m ruff check src tests

dryrun:
	$(PYTHON) -m src.cli ingest hh --dry

clean:
	$(PYTHON) -c "import shutil, pathlib; \
[shutil.rmtree(d, ignore_errors=True) for d in ('.pytest_cache', '.ruff_cache', '.mypy_cache')]; \
[shutil.rmtree(p, ignore_errors=True) for p in pathlib.Path('.').rglob('__pycache__') if not any(part in {'node_modules', '.next', 'master', 'derived', 'archive'} for part in p.parts)]"
