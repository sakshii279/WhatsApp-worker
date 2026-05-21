#!/bin/bash
cp config.azure.yaml config.yaml
pip install -r requirements.txt --quiet
gunicorn --bind 0.0.0.0:8000 --timeout 120 --workers 1 worker:app
