.PHONY: help install install-dev install-test install-all lint format typecheck test test-cov clean pre-commit-install pre-commit-run build dist-check docs api-docs

help:
	@echo "Sirius Chat - Development Commands"
	@echo "=================================="
	@echo ""
	@echo "Installation:"
	@echo "  make install          Install package in production mode"
	@echo "  make install-dev      Install with development tools"
	@echo "  make install-test     Install with test dependencies"
	@echo "  make install-all      Install all optional dependencies"
	@echo ""
	@echo "Code Quality:"
	@echo "  make lint             Run linters (pylint, flake8)"
	@echo "  make format           Format code with black and isort"
	@echo "  make typecheck        Run type checking with mypy"
	@echo "  make pre-commit-install  Install pre-commit hooks"
	@echo "  make pre-commit-run   Run all pre-commit hooks"
	@echo ""
	@echo "Testing:"
	@echo "  make test             Run all tests"
	@echo "  make test-cov         Run tests with coverage report"
	@echo ""
	@echo "Documentation:"
	@echo "  make api-docs         Generate API documentation (markdown + JSON)"
	@echo ""
	@echo "Build & Distribution:"
	@echo "  make build            Build distribution packages"
	@echo "  make dist-check       Check distribution packages"
	@echo ""
	@echo "Cleanup:"
	@echo "  make clean            Remove build artifacts and cache"

# Installation targets
install:
	pip install -e .

install-dev:
	pip install -e .[dev]

install-test:
	pip install -e .[test]

install-all:
	pip install -e .[dev,provider,quality]

# Code quality targets
lint:
	@echo "Running pylint..."
	-pylint sirius_pulse --fail-under=7.5 --disable=C0111,W0212
	@echo "Running flake8..."
	-flake8 sirius_pulse tests --max-line-length=100 --extend-ignore=E203,W503

format:
	@echo "Formatting with black..."
	black sirius_pulse tests
	@echo "Sorting imports with isort..."
	isort sirius_pulse tests

typecheck:
	@echo "Running mypy type checking..."
	-mypy sirius_pulse --ignore-missing-imports

# Pre-commit hooks
pre-commit-install:
	pre-commit install
	@echo "Pre-commit hooks installed successfully"

pre-commit-run:
	pre-commit run --all-files

# Testing targets
test:
	pytest -q

test-cov:
	pytest -q --cov=sirius_pulse --cov-report=html --cov-report=term-missing
	@echo "Coverage report generated in htmlcov/index.html"

# Build targets
build:
	python -m build

dist-check:
	python -m twine check dist/*

# Cleanup target
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "build" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "dist" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "htmlcov" -exec rm -rf {} + 2>/dev/null || true
	@echo "Clean completed"

# Documentation targets
api-docs:
	@echo "Generating API documentation..."
	python scripts/generate_api_docs.py markdown docs/api.md
	python scripts/generate_api_docs.py json docs/api.json
	@echo "API documentation generated in docs/"
