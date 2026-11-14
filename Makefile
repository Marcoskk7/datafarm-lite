.PHONY: test lint audit
PYTHON ?= python

test:
	CUDA_VISIBLE_DEVICES='' PYTHONPATH=src:tests $(PYTHON) -m pytest -q tests
lint:
	$(PYTHON) tools/lint_standing_rules.py
	PYTHONPATH=src $(PYTHON) -c 'from importlinter.cli import lint_imports_command; lint_imports_command()'
audit:
	$(PYTHON) tools/audit_instruction_layers.py
