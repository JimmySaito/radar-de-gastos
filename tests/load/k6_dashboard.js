// Prueba de carga de la ruta de lectura (modelo de lectura CQRS en DynamoDB).
//
//   k6 run -e BASE_URL=... -e TOKEN=$ID_TOKEN -e SCENARIO=stress tests/load/k6_dashboard.js
//
// TOKEN: id token de un usuario del dashboard (contiene custom:tenant_id).
import http from "k6/http";
import { check, group } from "k6";

const BASE_URL = __ENV.BASE_URL;
const headers = { authorization: `Bearer ${__ENV.TOKEN || ""}` };

const scenarios = {
  load: { executor: "constant-arrival-rate", rate: 100, timeUnit: "1s", duration: "10m", preAllocatedVUs: 50, maxVUs: 400 },
  stress: {
    executor: "ramping-arrival-rate", startRate: 20, timeUnit: "1s", preAllocatedVUs: 100, maxVUs: 1500,
    stages: [{ target: 200, duration: "2m" }, { target: 500, duration: "3m" }, { target: 1000, duration: "3m" }, { target: 0, duration: "1m" }],
  },
};

export const options = {
  scenarios: { main: scenarios[__ENV.SCENARIO || "load"] },
  thresholds: {
    "http_req_duration{route:summary}": ["p(95)<800"],
    "http_req_duration{route:timeseries}": ["p(95)<800"],
    http_req_failed: ["rate<0.01"],
  },
  summaryTrendStats: ["avg", "med", "p(90)", "p(95)", "p(99)", "max"],
};

const RANGES = [7, 30, 90];

export default function () {
  const days = RANGES[Math.floor(Math.random() * RANGES.length)];
  const to = new Date(Date.now() + 86400000);
  const from = new Date(to.getTime() - days * 86400000);
  const q = `from=${from.toISOString().slice(0, 10)}&to=${to.toISOString().slice(0, 10)}`;
  group("dashboard", () => {
    const s = http.get(`${BASE_URL}/v1/summary?${q}`, { headers, tags: { route: "summary" } });
    check(s, { "summary 200": (r) => r.status === 200 });
    const t = http.get(`${BASE_URL}/v1/timeseries?${q}&group_by=provider`, { headers, tags: { route: "timeseries" } });
    check(t, { "timeseries 200": (r) => r.status === 200 });
  });
}
