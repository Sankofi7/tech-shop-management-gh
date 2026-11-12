# Tech Shop Management System — Project Instructions

## 1. Project Overview

This is a Django-based Tech Shop Management System for a shop selling gaming consoles, gaming accessories, controllers, laptops, phones, and related accessories.

The system uses:

- Python and Django
- PostgreSQL
- HTML templates
- CSS and JavaScript
- Django authentication and role-based access control

The business operates in Ghana. Use Ghana cedis (GH₵ / GHS) for monetary values.

## 2. Development Principles

- Preserve the existing architecture and coding conventions.
- Inspect relevant files before making changes.
- Understand existing services, models, views, forms, templates, and tests before modifying them.
- Prefer small, focused changes over unnecessary rewrites.
- Reuse existing services and shared business logic.
- Avoid duplicate calculations and duplicated validation.
- Do not introduce unnecessary dependencies.
- Keep the interface responsive, clear, accessible, and consistent with the existing design.
- Explain important technical decisions in simple language when reporting work.

## 3. Git and Existing Work

- Always inspect `git status --short` before starting.
- Preserve all existing modified and untracked files.
- Never discard, reset, overwrite, or revert unrelated user changes.
- Do not stage or commit changes unless explicitly instructed by the user.
- Do not assume existing uncommitted work belongs to the current task.
- Keep changes limited to the requested feature.

## 4. Business Rules and Security

The application has three roles:

- OWNER
- SECRETARY
- EMPLOYEE

Respect existing role permissions and enforce authorization on the server side, not only in templates.

General rules:

- Employees must not access restricted company-wide financial information.
- Employees cannot approve their own credit sales.
- Users cannot promote themselves or bypass account permissions.
- Sensitive operations must use appropriate POST requests and CSRF protection.
- Validate all user input on the server.
- Never trust prices, totals, balances, stock quantities, or permissions supplied by the browser.
- Use Django transactions and appropriate row locking for critical financial and inventory operations.
- Prevent duplicate submissions where financial or stock changes could occur.
- Keep audit records for important financial, inventory, and account actions.
- Never silently overwrite or delete important financial records.
- Use reversal or adjustment workflows with reasons when corrections are necessary.

## 5. Sales, Payments, and Credit

- The server must calculate sale totals using stored product prices.
- Cash sales must follow existing payment rules.
- Employee-created credit sales require approval.
- Pending and rejected credit sales must not be counted as approved revenue.
- Pending MoMo payments must not be counted as confirmed payments.
- MoMo payments require the existing authorized confirmation workflow.
- Prevent overpayments and duplicate payment confirmations.
- Outstanding debt must use the shared customer ledger rather than separate calculations.

## 6. Inventory

- Do not change stock quantities directly through ordinary model updates.
- Use the established inventory movement services.
- Record the actor, reason, reference, previous quantity, resulting quantity, and timestamp where applicable.
- Prevent negative stock.
- Respect serial-number and IMEI tracking for individually tracked devices.
- Do not silently alter inventory movement history.

## 7. Customers, Returns, Refunds, and Exchanges

- Reuse the shared customer ledger for customer debt and credit calculations.
- Respect existing return eligibility and return-window rules.
- Do not treat pending or rejected returns as approved customer credits.
- Refunds must be checked against the current customer ledger.
- Pending MoMo refund disbursements must not be counted as paid.
- Preserve existing separation-of-duties rules.
- Exchanges must follow existing replacement-sale validation.
- Preserve historical return, refund, and exchange records.

## 8. Database Safety

There are two important PostgreSQL databases:

Production/application database:

- `tech_shop_db`

Test sandbox database:

- `tech_shop_test_sandbox`

Rules:

- Never run tests against `tech_shop_db`.
- Use the configured test sandbox when running PostgreSQL tests.
- Never modify production records for testing.
- Never expose or request database passwords in chat.
- Do not change database roles, permissions, credentials, or environment files without explicit approval.
- Do not create, apply, or remove migrations without explaining the need and obtaining approval.
- Check for migration drift when relevant.

## 9. Testing

Use targeted tests for the feature being changed.

For the complete SQLite-compatible suite:

`python manage.py test --settings=config.test_settings --verbosity 1`

For the PostgreSQL suite, use the approved test sandbox configuration:

`python manage.py test --settings=config.settings --verbosity 1`

Also use:

`python manage.py check --settings=config.settings`

And:

`python manage.py makemigrations --check --dry-run --settings=config.settings`

Do not claim a test passed unless it was actually executed and passed.

PostgreSQL tests are important for database locking and concurrency behavior. Do not rely only on SQLite tests for critical financial or inventory workflows.

## 10. Reporting and Dashboards

- Respect role-based data visibility.
- Use approved sales for revenue reporting.
- Use confirmed payments for received-payment reporting.
- Use the shared customer ledger for debt calculations.
- Do not count pending transactions as confirmed financial activity.
- Do not introduce expense or profit calculations until the user explicitly approves implementation of the Expenses Management module.

## 11. Scope Control

- Do not introduce supplier or purchasing modules unless requested.
- Do not introduce a separate Payments Management module unless requested.
- Do not implement autonomous AI actions that modify financial records, issue refunds, write off debts, or adjust stock.
- Future AI features should initially provide recommendations, alerts, and analysis only.
- Do not change established business policies without asking the user.

## 12. Required Workflow for Every Task

1. Understand the user's requested change.
2. Inspect Git status and relevant files.
3. Identify existing services and business rules that apply.
4. Explain any important risk or ambiguity before making a major change.
5. Implement only the requested scope.
6. Add or update appropriate tests.
7. Run targeted tests.
8. Run broader tests when appropriate.
9. Check Django system checks and migration drift where relevant.
10. Inspect `git diff --check` and `git status --short`.
11. Report:
    - What was inspected.
    - What was changed.
    - Tests actually executed and their results.
    - Any remaining limitations or risks.
    - Whether migrations were created or applied.
    - Whether any database or Git operations were performed.

Do not stage or commit unless the user explicitly requests it.
