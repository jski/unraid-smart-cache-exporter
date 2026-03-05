.PHONY: fmt lint test docker-build run

fmt:
	python3 -m compileall exporter.py tests

lint:
	python3 -m py_compile exporter.py

test:
	python3 -m unittest discover -s tests -p 'test_*.py' -v

docker-build:
	docker build -t unraid-smart-cache-exporter:dev .

run:
	python3 exporter.py
