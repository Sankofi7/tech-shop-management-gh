# Tech Shop Management System

Django application for a Ghanaian shop selling gaming consoles, controllers,
accessories, laptops, and phones. Monetary amounts are recorded in Ghana cedis
(GHS), using decimal fields.

## Current modules

- **Accounts:** Owner, secretary, and employee roles.
- **Products and inventory:** Categories, brands, products, serialized units,
  stock movements, and low-stock levels.
- **Sales:** Responsive point of sale, cash and credit sales, payment history,
  credit approval, MTN MoMo confirmation, receipts, and searchable sale history.
- **Customers and debt tracking:** Customer profiles, customer-linked sales,
  approved credit balances, allocated repayments, repayment receipts, and
  pending MTN MoMo review.
- **Audit:** Append-only records of sales, approval decisions, and payment
  actions.

Supplier records and advanced financial reports have not been implemented.

## Local setup

Requirements: Python 3.13, PostgreSQL 18.6 (Postgres.app), and Git. The
project's .venv environment and local PostgreSQL database are already set up
on this development machine. Start PostgreSQL before using the application.

~~~sh
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp -n .env.example .env
chmod 600 .env
# Set DB_PASSWORD in .env using a local editor; keep it private.
python manage.py check --database default
~~~

The custom account, products, inventory, sales (including the POS checkout retry
key), customers, and audit migrations have been applied to the local PostgreSQL
database. On a new database, set its private
environment variables first, then run:

~~~sh
python manage.py migrate
python manage.py createsuperuser
~~~

The custom user manager assigns the OWNER role to the interactively created
superuser. Give each account its own unique password. Do not put real secrets
in .env.example or source files. Git ignores .env and .venv.

## Start the application

~~~sh
source .venv/bin/activate
python manage.py runserver 127.0.0.1:8000
~~~

Open http://127.0.0.1:8000/admin/ to manage product records and view
sales, payment, and audit history. Use http://127.0.0.1:8000/sales/ for
sales history, http://127.0.0.1:8000/sales/new/ to open the POS, and
http://127.0.0.1:8000/customers/ for customer profiles and debt views. Stop
the development server with Ctrl+C.

The application dashboard is at `/` and the shared sign-in screen is at
`/accounts/login/`. Sign-out uses a CSRF-protected POST action. Product,
inventory, reports, and settings navigation entries are labeled as coming soon
until their dedicated frontend screens are implemented.

Employees can record cash sales and submit credit sales for approval. A pending
credit sale does not change stock until an owner or secretary approves it.
MTN MoMo entries remain pending until a manager records the confirmation
reference after checking the transaction. Confirmed payments alone count as
money received.

Credit sales require a customer. Customer debt is calculated from approved
credit sales and confirmed payments. Repayments use the existing immutable
sales payment ledger and are linked to one credit sale; pending MoMo entries
do not reduce debt until confirmed.

## Checks and tests

~~~sh
python manage.py check --database default
python manage.py test apps.accounts apps.products apps.sales apps.customers apps.payments apps.audit --settings=config.test_settings
python manage.py makemigrations --check --dry-run
~~~

The test settings use an isolated in-memory SQLite database, so tests do not
need additional database-creation privileges on the local PostgreSQL role.
