PY := python/.venv/bin/python

.PHONY: setup regen-model regen-model-check test lint docs docs-serve docs-check

# Use uv because system python 3.14 ships without ensurepip on this box.
setup:
	cd python && uv venv --python 3.14 .venv
	uv pip install --python python/.venv/bin/python -e python[dev]

regen-model:
	$(PY) scripts/regen_model.py

regen-model-check:
	$(PY) scripts/regen_model.py --check --skip-fetch

test:
	cd python && .venv/bin/pytest

lint:
	PATH=$(CURDIR)/python/.venv/bin:$$PATH prek run --all-files

docs:
	cd python && .venv/bin/mkdocs build

docs-serve:
	cd python && .venv/bin/mkdocs serve

docs-check:
	cd python && .venv/bin/mkdocs build --strict
