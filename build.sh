#!/usr/bin/env bash
set -e

# Install dependencies
pip install -r requirements.txt

# Collect static files
python manage.py collectstatic --noinput --ignore="*.map"

# Use direct database connection for DDL operations
if [ -n "$DIRECT_DATABASE_URL" ]; then
    export DATABASE_URL="$DIRECT_DATABASE_URL"
fi

python manage.py migrate
