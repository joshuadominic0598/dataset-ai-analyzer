.PHONY: start demo test evaluate sample check-ui

start:
	python3 -m reviso

demo:
	python3 -m reviso --demo

test:
	python3 -m unittest discover -s tests -v

evaluate:
	python3 scripts/evaluate.py
	python3 scripts/evaluate_single.py

sample:
	python3 scripts/make_demo.py

check-ui: evaluate
	node --check reviso/static/app.js
	node scripts/check_ui.mjs
