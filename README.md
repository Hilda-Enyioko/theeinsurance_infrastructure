# TheeInsurance API

A headless, API-first insurance distribution platform. Companies integrate TheeInsurance into their products via API and offer insurance plans to their customers without building the insurance layer themselves.

## Tech Stack
- Django + Django REST Framework
- SimpleJWT authentication
- SQLite (development) / PostgreSQL (production)

## Setup

```bash
git clone <repo-url>
cd theeinsurance-api
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file using `.env.example` as reference, then:

```bash
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

## Project Status
In active development. Currently building core partner infrastructure.

## Author
Hilda Enyioko