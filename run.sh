#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
pip install -q -r requirements.txt
[ -f .env ] || cp .env.example .env
python seed.py --if-empty
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers --forwarded-allow-ips="*"
