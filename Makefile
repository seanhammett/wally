PY := .venv/bin/python

.PHONY: setup build rebuild validate serve deploy clean-processed

setup:                     ## create the venv and install Python dependencies
	python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
	@echo "Also required: brew install tippecanoe && npm i -g mapshaper"

build:                     ## fetch, transform, validate, join, tile, write the manifest
	$(PY) pipeline/build.py

rebuild:                   ## rebuild from what is already in data/raw
	$(PY) pipeline/build.py --skip-fetch

validate:                  ## check data/processed against the output contract
	$(PY) pipeline/validate.py

serve:                     ## serve the built site
	$(PY) pipeline/serve.py --port 8000

deploy:                    ## publish site/ to the gh-pages branch (GitHub Pages)
	pipeline/deploy.sh

clean-processed:           ## drop intermediates; data/raw is never touched
	rm -rf data/processed data/scratch site/tiles site/stats site/files site/layers.json
