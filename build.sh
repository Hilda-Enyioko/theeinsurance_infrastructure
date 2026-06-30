#!/usr/bin/env bash
pip install -r requirements.txt
DATABASE_URL="$DIRECT_DATABASE_URL" python manage.py migrate