#!/usr/bin/env bash
pip install -r requirements.txt && python manage.py collectstatic --noinput
DATABASE_URL="$DIRECT_DATABASE_URL" python manage.py migrate