# Top-level convenience.  All targets pass through to the bootstrap
# (Python) compiler's Makefile in `bootstrap/`.  Once a self-hosted
# Ouro compiler lands, equivalent targets for it will go here too —
# `make check` will fan out to both.
#
# FILE paths are resolved to absolute before forwarding so the
# bootstrap Makefile (which `cd`s into bootstrap/ to invoke uv)
# can find files specified relative to the repo root.

ROOT := $(abspath .)
# Convert a possibly-relative FILE to absolute relative to the repo
# root.  If FILE is already absolute, `abspath` is a no-op.
FILE_ABS := $(if $(FILE),$(abspath $(FILE)),)

.PHONY: help sync test test-py test-ouro lint fmt ty check clean ir build run

help:
	@$(MAKE) -C bootstrap help

sync test test-py test-ouro lint fmt ty check clean ir build run:
	@$(MAKE) -C bootstrap $@ FILE=$(FILE_ABS) ARGS=$(ARGS)
