PYTHON ?= ../../ingestion-service/.venv/bin/python
RUFF ?= ../../chat-service/.venv/bin/ruff
GOLD_REVIEW ?= gold-review-v2.template.json
RELEVANCE_REVIEW ?= tuning-relevance-v2.template.json

.PHONY: validate validate-v2 gold-review-v2 relevance-review-v2 freeze-check-v2 freeze-dataset-v2 arm-cpu-audit-docker arm-cpu-single-thread-audit-docker arm-startup-diagnostic-docker parser-audit-v2 kis-persistence-v2-docker retrieval-restricted-v2-docker retrieval-load-v2-docker test lint format-check parser-audit parser-chunk-audit parser-chunk-audit-docker kis-persistence-audit-docker structure-audit-docker inventory
validate:
	$(PYTHON) runner.py validate
validate-v2:
	$(PYTHON) validate_revision_v2.py
gold-review-v2:
	$(PYTHON) gold_review_v2.py
relevance-review-v2:
	$(PYTHON) retrieval_metrics_v2.py check
freeze-check-v2:
	$(PYTHON) freeze_dataset_v2.py check --gold-review "$(GOLD_REVIEW)" --labels "$(RELEVANCE_REVIEW)"
freeze-dataset-v2:
	$(PYTHON) freeze_dataset_v2.py freeze --gold-review "$(GOLD_REVIEW)" --labels "$(RELEVANCE_REVIEW)"
arm-cpu-audit-docker: validate-v2
	$(PYTHON) arm_cpu_audit.py
arm-cpu-single-thread-audit-docker: validate-v2
	$(PYTHON) arm_cpu_audit.py --single-thread
arm-startup-diagnostic-docker: validate-v2
	$(PYTHON) arm_cpu_audit.py --diagnose-startup --single-thread
parser-audit-v2: validate-v2
	$(PYTHON) audit_revision_v2.py
kis-persistence-v2-docker: validate-v2
	$(PYTHON) isolated_revision_v2.py
retrieval-restricted-v2-docker: validate-v2
	$(PYTHON) isolated_retrieval_v2.py
retrieval-load-v2-docker: validate-v2
	U0_RETRIEVAL_LOAD=1 $(PYTHON) isolated_retrieval_v2.py
test:
	$(PYTHON) -m unittest discover -s tests -v
lint:
	$(RUFF) check . --select E,F,I,B,UP --target-version py311
format-check:
	$(RUFF) format --check .
parser-audit:
	$(PYTHON) audit_parsers.py
parser-chunk-audit:
	HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 $(PYTHON) audit_parsers.py --chunks
parser-chunk-audit-docker: validate
	$(PYTHON) docker_parser_audit.py
kis-persistence-audit-docker: validate
	$(PYTHON) isolated_kis_audit.py
structure-audit-docker: validate
	$(PYTHON) structure_audit.py
inventory:
	$(PYTHON) host_inventory.py
