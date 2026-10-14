CARGO ?= ./scripts/repo-cargo
UV ?= uv
PACKAGE_OUTPUT ?= .state/packages/$(shell date -u +%Y%m%dT%H%M%SZ)
RELEASE_OUTPUT ?= .state/releases/$(shell date -u +%Y%m%dT%H%M%SZ)
RELEASE_ARGS ?=
NATIVE_CONSOLE_PREPARE_ARGS ?=
NATIVE_CONSOLE_BUILD_ARGS ?=
NATIVE_CONSOLE_ARGS ?=

.PHONY: build check test test-rust test-python lint lint-rust lint-python fmt worker-browser smoke smoke-stack smoke-desktop smoke-pdf smoke-desktop-stack smoke-desktop-admin ui-deps ui-build ui-check docs-check package-check release-check release-artifacts native-console-prepare smoke-native-console clean

build:
	$(CARGO) build --locked

check:
	$(CARGO) check --locked --all-targets

test: test-rust test-python

test-rust:
	$(CARGO) test --locked

test-python:
	$(UV) run --locked --group dev pytest tests/python infra/tests -m 'not browser'

lint: lint-rust lint-python

lint-rust:
	$(CARGO) fmt --all -- --check
	$(CARGO) clippy --locked --all-targets -- -D warnings

lint-python:
	$(UV) run --locked --group dev ruff check python tests examples scripts infra

fmt:
	$(CARGO) fmt --all

worker-browser:
	$(UV) run --locked playwright install chromium

ui-deps:
	npm --prefix ui/mcp-app ci --ignore-scripts --no-audit --no-fund

ui-build: ui-deps
	npm --prefix ui/mcp-app run build

ui-check: ui-deps
	npm --prefix ui/mcp-app run check

docs-check:
	npm --prefix docs ci --ignore-scripts --no-audit --no-fund
	npm --prefix docs run check

package-check:
	$(UV) run --locked --group build python scripts/package_artifacts.py check --output "$(PACKAGE_OUTPUT)" --uv "$(UV)"

# Hosted-CI equivalent. Chromium must already be installed (make worker-browser).
# Linux desktop/Docker, native Console and live consumer acceptance stay explicit.
release-check: check test lint ui-check docs-check smoke smoke-stack package-check

# Prepare private files only. No tag, release, registry or cloud write is performed.
release-artifacts:
	$(UV) run --locked --group build python scripts/package_artifacts.py bundle --output "$(RELEASE_OUTPUT)" --uv "$(UV)" $(RELEASE_ARGS)

# Explicit native consumer acceptance uses supplied binaries and never compiles Rust.
native-console-prepare:
	$(UV) run --locked python scripts/prepare_native_console.py $(NATIVE_CONSOLE_PREPARE_ARGS)

smoke-native-console:
	$(UV) run --locked python scripts/test_native_console.py $(NATIVE_CONSOLE_ARGS)

.PHONY: native-console-build
native-console-build:
	$(UV) run --locked python scripts/build_native_console.py $(NATIVE_CONSOLE_BUILD_ARGS)

smoke:
	$(UV) run --locked --group dev pytest tests/python -m browser

smoke-stack:
	$(CARGO) test --locked --test stack -- --ignored --nocapture
	$(CARGO) test --locked --test transfer_stack -- --ignored --nocapture

smoke-desktop:
	./scripts/test-desktop.sh

smoke-pdf:
	DESKTOP_TEST_SUITE=pdf ./scripts/test-desktop.sh

smoke-desktop-stack:
	./scripts/test-desktop-stack.sh

smoke-desktop-admin:
	./scripts/test-desktop-admin.sh

clean:
	$(CARGO) clean
