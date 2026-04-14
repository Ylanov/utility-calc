# Load Tests

This directory contains HTTP load tests for the platform.

The pytest performance suite in `app/tests/performance` is useful for code-level regression checks.
These k6 scripts are for API-level pressure testing against a running instance with a real database.

## Recommended order

1. Run `pytest -m perf app/tests/performance -q`
2. Seed the application with realistic data
3. Run the k6 scripts below against a staging environment

## 1. Mixed admin and resident traffic

```bash
k6 run load_tests/k6_admin_resident_mix.js \
  -e BASE_URL=http://localhost:8000 \
  -e ADMIN_USERNAME=admin \
  -e ADMIN_PASSWORD=admin_password \
  -e USER_USERNAME=test_user \
  -e USER_PASSWORD=test_password
```

This script stresses the hottest read-only routes:

- `/api/token`
- `/api/admin/dashboard`
- `/api/admin/summary`
- `/api/admin/readings`
- `/api/users`
- `/api/rooms`
- `/api/readings/state`
- `/api/readings/history`

## 2. Authentication spike

```bash
k6 run load_tests/k6_auth_spike.js \
  -e BASE_URL=http://localhost:8000 \
  -e ADMIN_USERNAME=admin \
  -e ADMIN_PASSWORD=admin_password
```

This is useful when you want to understand the cost of:

- password verification
- JWT generation
- rate limiting
- database lookup during login

## Safety notes

- Do not point these scripts at destructive endpoints during load tests.
- Run them against staging first, not production.
- Keep the staging dataset close to real usage: rooms, readings, users, drafts, and history depth.

## Useful env overrides

- `ADMIN_VUS`
- `ADMIN_TABLE_VUS`
- `USER_VUS`
- `AUTH_SPIKE_VUS`
- `DURATION`
- `BASE_URL`

