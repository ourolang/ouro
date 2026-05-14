unexport VIRTUAL_ENV

# Bootstrap (Python) compiler Makefile.  Lives at <repo>/bootstrap/.
# Paths are anchored to this directory; runtime/std/test-ouro are
# at the repo root one level up.
UV       := uv
SRC      := src
PYTEST   := test
REPO     := ..
OURO     := $(REPO)/test
OUT      := out

QBE      := qbe
CC       := cc

# Freestanding flags applied to the final link.  The runtime is now
# pure Ouro (std/runtime/*.ou); nothing C to compile here.
FREESTANDING_FLAGS := -O2 -ffreestanding -fno-stack-protector \
                      -fno-builtin -fno-pic -fno-pie -no-pie


.PHONY: help sync test test-py test-ouro lint fmt ty check clean ir build run

help:
	@echo "Targets:"
	@echo "  sync          install dev dependencies via uv"
	@echo "  test          run all tests (python + ouro)"
	@echo "  test-py       run only the Python unit/integration tests"
	@echo "  test-ouro     run only the .ou test programs"
	@echo "  lint          ruff check on src/ and test/python/"
	@echo "  fmt           ruff format on src/ and test/python/"
	@echo "  ty            ty check on src/ and test/python/"
	@echo "  check         test + lint + ty"
	@echo "  ir   FILE=…   compile FILE, print QBE IR to stdout"
	@echo "  build FILE=…  compile FILE end-to-end → executable in $(OUT)/"
	@echo "  run   FILE=…  build and run FILE (ARGS=… forwards args)"
	@echo "  clean         remove __pycache__ / .pytest_cache / $(OUT)/"

sync:
	$(UV) sync --group dev

test:
	$(UV) run pytest $(PYTEST) -v

test-py:
	$(UV) run pytest $(PYTEST) --ignore=$(PYTEST)/test_ouro.py -v

test-ouro:
	$(UV) run pytest $(PYTEST)/test_ouro.py -v

lint:
	$(UV) run ruff check $(SRC) $(PYTEST)

fmt:
	$(UV) run ruff format $(SRC) $(PYTEST)

ty:
	$(UV) run ty check $(SRC) $(PYTEST)

check: test lint ty

# Compile FILE to QBE IR, print to stdout (debug / inspect).
ir:
	@if [ -z "$(FILE)" ]; then echo "usage: make ir FILE=examples/02_hello_world.ou"; exit 1; fi
	@$(UV) run python -m src.main $(FILE)

# Compile FILE end-to-end: Ouro -> QBE IR -> assembly -> static executable.
# Linked with -static -nostdlib; the auto-loaded runtime/*.ou modules
# supply everything (printf, ARC, allocator, _start, syscalls).  When
# FILE (or any runtime module) contains `asm` declarations, the asm
# bodies arrive in .asm.s and get appended to QBE's output before the
# assembler runs.
build:
	@if [ -z "$(FILE)" ]; then echo "usage: make build FILE=examples/02_hello_world.ou"; exit 1; fi
	@mkdir -p $(OUT)
	@name=$$(basename $(FILE) .ou); \
	  $(UV) run python -m src.main $(FILE) \
	    --asm-out $(OUT)/$$name.asm.s > $(OUT)/$$name.ssa && \
	  $(QBE) -o $(OUT)/$$name.s $(OUT)/$$name.ssa && \
	  cat $(OUT)/$$name.asm.s >> $(OUT)/$$name.s && \
	  $(CC) $(FREESTANDING_FLAGS) -static -nostdlib \
	    -o $(OUT)/$$name $(OUT)/$$name.s && \
	  echo "built $(OUT)/$$name"

# Build and run FILE.  Forwards args via ARGS=...; reports exit code.
run: build
	@name=$$(basename $(FILE) .ou); \
	  $(OUT)/$$name $(ARGS); \
	  echo "exit=$$?"

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +
	find . -name "*.pyc" -delete
	rm -rf $(OUT)
