#!/bin/bash
# startup.sh
# Run by Azure App Service on container start.
# Installs dependencies and starts gunicorn pointing to worker:app

pip install -r requirements.txt --quiet
gunicorn --bind 0.0.0.0:8000 --timeout 120 --workers 1 worker:app
