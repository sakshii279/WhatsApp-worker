#!/bin/bash
cp /home/site/wwwroot/config.azure.yaml /home/site/wwwroot/config.yaml
pip install -r requirements.txt --quiet
gunicorn --bind 0.0.0.0:8000 --timeout 120 --workers 1 worker:app
