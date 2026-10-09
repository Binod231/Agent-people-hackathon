# SAM build recipe for the one Lambda function in infra/template.yaml.
#
# This file exists to solve one problem: the function serves both halves of the
# product, so the deployment package needs the Python in `src/agent/` *and* the
# three Chat_UI files in `ui/`, which live in different trees in the repository.
# `infra/template.yaml` declares `Metadata: BuildMethod: makefile` with
# `CodeUri: ../`, which makes `sam build` call the `build-AgentFunction` target
# below instead of guessing a Python layout, and this target decides the layout.
#
# The layout it produces -- the handler in task 5.2 must match it:
#
#   $(ARTIFACTS_DIR)/
#   |- agent/                   <- the package; Handler: agent.handler.handler
#   |  |- handler.py
#   |  |- bedrock.py, config.py, ... every *.py from src/agent/
#   |  `- ui/                   <- Path(__file__).resolve().parent / "ui"
#   |     |- index.html
#   |     |- styles.css
#   |     `- app.js
#   `- boto3/, botocore/, ...   <- pinned from src/requirements.txt
#
# So the assets sit *inside* the package, one directory below the module that
# reads them, and the handler resolves them relative to its own file rather
# than to a working directory it does not control:
#
#   _UI_DIR = Path(__file__).resolve().parent / "ui"
#
# In a source checkout that path does not exist -- `src/agent/ui/` is not a
# directory, `ui/` at the repository root is. `scripts/local_server.py` (also
# task 5.2) needs a second candidate for that case, which is two lines:
#
#   _CANDIDATES = (Path(__file__).resolve().parent / "ui",            # packaged
#                  Path(__file__).resolve().parents[2] / "ui")        # checkout
#
# `parents[2]` from `src/agent/handler.py` is the repository root. Pick the
# first candidate that exists, at import, once.
#
# Why the assets go inside the package rather than beside it: `sys.path` on
# Lambda contains the package root, so a sibling `ui/` directory would be
# resolvable only by walking up from `__file__` anyway, and a top-level `ui`
# directory in the artifact is one name collision away from a third-party
# package of the same name. Inside the package the path is unambiguous.
#
# Run it through SAM, not directly -- SAM sets ARTIFACTS_DIR. From the
# repository root, with an absolute config path (`sam build` resolves a
# relative --config-file against the template's directory, not the working
# directory, and reports the file as missing):
#
#   sam build --template-file "$PWD/infra/template.yaml" \
#             --config-file   "$PWD/infra/samconfig.toml"
#
# Warnings below print to stdout rather than stderr on purpose: SAM captures a
# successful build's stderr and shows it only on failure, so a warning sent
# there would be invisible exactly when it matters.
#
# SAM copies the CodeUri tree to a scratch directory and runs make there unless
# `build_in_source` is set (infra/samconfig.toml sets it, which skips the copy).
# Either way the working directory is the repository root, so every path below
# is relative to it.

PYTHON ?= python3
UI_FILES := index.html styles.css app.js

.PHONY: build-AgentFunction

build-AgentFunction:
	@test -n "$(ARTIFACTS_DIR)" || { \
	  echo "ARTIFACTS_DIR is unset. Run this through 'sam build', not 'make'." >&2; \
	  exit 1; \
	}
	@test -d src/agent || { \
	  echo "src/agent not found. Run 'sam build' from the repository root." >&2; \
	  exit 1; \
	}
	"$(PYTHON)" -m pip install \
	  --requirement src/requirements.txt \
	  --target "$(ARTIFACTS_DIR)" \
	  --no-compile --upgrade --quiet
	mkdir -p "$(ARTIFACTS_DIR)/agent/ui"
	cp src/agent/*.py "$(ARTIFACTS_DIR)/agent/"
	@for f in $(UI_FILES); do \
	  if [ -f "ui/$$f" ]; then \
	    cp "ui/$$f" "$(ARTIFACTS_DIR)/agent/ui/$$f"; \
	  else \
	    echo "WARNING: ui/$$f does not exist yet -- not in this package."; \
	    echo "         The function will 404 that path until task 8.1 or 8.2"; \
	    echo "         writes it. scripts/deploy.sh refuses to deploy without"; \
	    echo "         all three files, so this is a warning only while the"; \
	    echo "         UI is still being written."; \
	  fi; \
	done
	@echo "packaged: $$(ls "$(ARTIFACTS_DIR)/agent" | tr '\n' ' ')"
	@echo "packaged ui: $$(ls "$(ARTIFACTS_DIR)/agent/ui" | tr '\n' ' ')"
