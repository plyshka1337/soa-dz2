.PHONY: generate up test

generate:
	python scripts/generate.py

up:
	docker compose up --build -d

test:
	docker compose exec -T api python -m unittest discover -s tests -v
