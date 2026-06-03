VENV    := venv
PYTHON  := $(VENV)/bin/python
PYI     := $(VENV)/bin/pyinstaller
SCRIPTS := download delete

.PHONY: all dist clean

all: dist

dist: $(addprefix dist/,$(SCRIPTS))

$(PYI): $(VENV)/bin/pip
	$(VENV)/bin/pip install pyinstaller

dist/%: %.py common.py $(PYI)
	$(PYI) --onefile --name $* $<

$(VENV)/bin/pip:
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -r requirements.txt

clean:
	rm -rf dist build *.spec __pycache__
