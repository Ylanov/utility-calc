import http from "k6/http";
import { check, sleep } from "k6";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";

export const options = {
  scenarios: {
    auth_spike: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "30s", target: Number(__ENV.AUTH_SPIKE_VUS || 20) },
        { duration: "1m", target: Number(__ENV.AUTH_SPIKE_VUS || 20) },
        { duration: "30s", target: 0 },
      ],
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.05"],
    "http_req_duration{name:login}": ["p(95)<1500"],
  },
};

export default function () {
  const response = http.post(
    `${BASE_URL}/api/token`,
    {
      username: __ENV.ADMIN_USERNAME || "admin",
      password: __ENV.ADMIN_PASSWORD || "admin_password",
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

  sleep(1);
}
