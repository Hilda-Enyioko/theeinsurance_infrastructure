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

# Reset plans app migration state and apply all migrations
python manage.py migrate plans zero
python manage.py migrate
