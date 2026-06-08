#!/bin/bash

# Start FastAPI dashboard backend on port 8001
cd /home/site/wwwroot/dashboard-backend
pip install -r requirements.txt --quiet
gunicorn main:app --workers 2 --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:8001 --daemon --log-file /tmp/dashboard.log

# Start WhatsApp worker on port 8000 (main port)
cd /home/site/wwwroot
gunicorn worker:app --workers 1 --bind 0.0.0.0:8000 --timeout 120
