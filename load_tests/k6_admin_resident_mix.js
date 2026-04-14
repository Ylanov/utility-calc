import http from "k6/http";
import { check, sleep } from "k6";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";
const DURATION = __ENV.DURATION || "2m";

function authHeaders(token) {
  return {
    Authorization: `Bearer ${token}`,
  };
}

function login(username, password) {
  const response = http.post(
    `${BASE_URL}/api/token`,
    {
      username,
      password,
      grant_type: "password",
    },
    {
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
      },
      tags: { name: "login" },
    }
  );

  check(response, {
    "login status is 200": (r) => r.status === 200,
    "login returned token": (r) => !!r.json("access_token"),
  });

  return response.json("access_token");
}

export const options = {
  scenarios: {
    admin_dashboard: {
      executor: "constant-vus",
      vus: Number(__ENV.ADMIN_VUS || 5),
      duration: DURATION,
      exec: "adminDashboardFlow",
    },
    admin_tables: {
      executor: "constant-vus",
      vus: Number(__ENV.ADMIN_TABLE_VUS || 5),
      duration: DURATION,
      exec: "adminTableFlow",
    },
    resident_flow: {
      executor: "constant-vus",
      vus: Number(__ENV.USER_VUS || 20),
      duration: DURATION,
      exec: "residentFlow",
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.03"],
    "http_req_duration{name:admin-dashboard}": ["p(95)<1500"],
    "http_req_duration{name:admin-summary}": ["p(95)<2000"],
    "http_req_duration{name:admin-readings}": ["p(95)<2000"],
    "http_req_duration{name:users-list}": ["p(95)<1800"],
    "http_req_duration{name:rooms-list}": ["p(95)<1800"],
    "http_req_duration{name:readings-state}": ["p(95)<1200"],
    "http_req_duration{name:readings-history}": ["p(95)<1200"],
  },
};

export function setup() {
  return {
    adminToken: login(
      __ENV.ADMIN_USERNAME || "admin",
      __ENV.ADMIN_PASSWORD || "admin_password"
    ),
    userToken: login(
      __ENV.USER_USERNAME || "test_user",
      __ENV.USER_PASSWORD || "test_password"
    ),
  };
}

export function adminDashboardFlow(data) {
  const params = { headers: authHeaders(data.adminToken), tags: { name: "admin-dashboard" } };
  const summaryParams = { headers: authHeaders(data.adminToken), tags: { name: "admin-summary" } };

  check(http.get(`${BASE_URL}/api/admin/dashboard`, params), {
    "dashboard status is 200": (r) => r.status === 200,
  });
  check(http.get(`${BASE_URL}/api/admin/summary`, summaryParams), {
    "summary status is 200": (r) => r.status === 200,
  });

  sleep(1);
}

export function adminTableFlow(data) {
  const headers = authHeaders(data.adminToken);

  check(
    http.get(`${BASE_URL}/api/admin/readings?page=1&limit=50&sort_by=created_at&sort_dir=desc`, {
      headers,
      tags: { name: "admin-readings" },
    }),
    {
      "admin readings status is 200": (r) => r.status === 200,
    }
  );

  check(
    http.get(`${BASE_URL}/api/users?page=1&limit=50`, {
      headers,
      tags: { name: "users-list" },
    }),
    {
      "users status is 200": (r) => r.status === 200,
    }
  );

  check(
    http.get(`${BASE_URL}/api/rooms?page=1&limit=50`, {
      headers,
      tags: { name: "rooms-list" },
    }),
    {
      "rooms status is 200": (r) => r.status === 200,
    }
  );

  sleep(1);
}

export function residentFlow(data) {
  const headers = authHeaders(data.userToken);

  check(
    http.get(`${BASE_URL}/api/readings/state`, {
      headers,
      tags: { name: "readings-state" },
    }),
    {
      "state status is 200": (r) => r.status === 200,
    }
  );

  check(
    http.get(`${BASE_URL}/api/readings/history`, {
      headers,
      tags: { name: "readings-history" },
    }),
    {
      "history status is 200": (r) => r.status === 200,
    }
  );

  sleep(1);
}

