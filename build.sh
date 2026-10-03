#!/usr/bin/env bash
set -e

pip install -r requirements.txt
python manage.py collectstatic --noinput --clear

if [ -n "$DIRECT_DATABASE_URL" ]; then
    export DATABASE_URL="$DIRECT_DATABASE_URL"
fi

python manage.py migrate