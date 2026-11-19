// Prueba de estrés de la ruta de escritura (API Gateway → Lambda → SQS → Lambda → DynamoDB).
//
//   k6 run -e BASE_URL=https://xxx.execute-api.us-east-1.amazonaws.com -e TOKEN=$TOKEN \
//          -e SCENARIO=stress tests/load/k6_ingest.js
//
// TOKEN: access token client_credentials del cliente M2M (scope radar-gastos/ingest):
//   curl -s -u "$CLIENT_ID:$CLIENT_SECRET" -d grant_type=client_credentials \
//        -d scope=radar-gastos/ingest "$COGNITO_DOMAIN/oauth2/token" | jq -r .access_token
import http from "k6/http";
import { check } from "k6";
import { Trend, Counter } from "k6/metrics";

const BASE_URL = __ENV.BASE_URL;
const TOKEN = __ENV.TOKEN || "";
const BATCH = Number(__ENV.BATCH || 25);
const RUN_ID = (__ENV.RUN_ID || `${Date.now()}`).slice(-8);

const scenarios = {
  // Carga nominal: 50 req/s (1.250 registros/s) durante 10 minutos.
  load: { executor: "constant-arrival-rate", rate: 50, timeUnit: "1s", duration: "10m", preAllocatedVUs: 50, maxVUs: 300 },
  // Estrés: rampa hasta encontrar el punto de quiebre (throttling 429 o errores 5xx).
  stress: {
    executor: "ramping-arrival-rate", startRate: 10, timeUnit: "1s", preAllocatedVUs: 100, maxVUs: 1000,
    stages: [
      { target: 100, duration: "2m" }, { target: 300, duration: "3m" },
      { target: 600, duration: "3m" }, { target: 1000, duration: "3m" }, { target: 0, duration: "1m" },
    ],
  },
  // Pico: de 20 a 500 req/s en 10 s (elasticidad y arranques en frío).
  spike: {
    executor: "ramping-arrival-rate", startRate: 20, timeUnit: "1s", preAllocatedVUs: 100, maxVUs: 1000,
    stages: [
      { target: 20, duration: "1m" }, { target: 500, duration: "10s" },
      { target: 500, duration: "2m" }, { target: 20, duration: "10s" }, { target: 20, duration: "2m" },
    ],
  },
  // Resistencia: carga moderada sostenida para detectar fugas o degradación.
  soak: { executor: "constant-arrival-rate", rate: 30, timeUnit: "1s", duration: "1h", preAllocatedVUs: 50, maxVUs: 200 },
};

export const options = {
  scenarios: { main: scenarios[__ENV.SCENARIO || "load"] },
  thresholds: {
    // Objetivos de calidad (ver QAS-P1 y QAS-A1 en el repositorio de arquitectura).
    "http_req_duration{route:ingest}": ["p(95)<500", "p(99)<1000"],
    http_req_failed: ["rate<0.01"],
  },
  summaryTrendStats: ["avg", "med", "p(90)", "p(95)", "p(99)", "max"],
};

const accepted = new Counter("records_accepted");
const throttled = new Counter("throttled_429");
const ingestLatency = new Trend("ingest_latency", true);

export default function () {
  const day = new Date().toISOString().slice(0, 10);
  const records = Array.from({ length: BATCH }, (_, i) => ({
    provider: "custom",
    service: `k6-${RUN_ID}`,
    principal: `user:vu${__VU}-it${__ITER}-${i}`,
    usage_date: day,
    input_tokens: Math.floor(Math.random() * 100000),
    output_tokens: Math.floor(Math.random() * 10000),
    requests: 1 + Math.floor(Math.random() * 20),
    cost: (Math.random() * 5).toFixed(4),
  }));
  const res = http.post(`${BASE_URL}/v1/usage/records`, JSON.stringify({ records }), {
    headers: { "content-type": "application/json", authorization: `Bearer ${TOKEN}` },
    tags: { route: "ingest" },
  });
  ingestLatency.add(res.timings.duration);
  if (res.status === 429) throttled.add(1);
  if (check(res, { "202 accepted": (r) => r.status === 202 })) accepted.add(BATCH);
}
