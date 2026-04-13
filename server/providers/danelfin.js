import fetch from "node-fetch";
const BASE_URL = "https://apirest.danelfin.com";

export async function getRanking({ apiKey, params }) {
  const qs = new URLSearchParams(params).toString();
  const url = `${BASE_URL}/ranking?${qs}`;
  const res = await fetch(url, { headers: { "x-api-key": apiKey } });
  if (!res.ok) throw new Error(`Upstream ${res.status}`);
  return res.json();
}

export async function getSectors({ apiKey }) {
  const r = await fetch(`${BASE_URL}/sectors`, { headers: { "x-api-key": apiKey } });
  if (!r.ok) throw new Error(`Upstream ${r.status}`);
  return r.json();
}

export async function getIndustries({ apiKey }) {
  const r = await fetch(`${BASE_URL}/industries`, { headers: { "x-api-key": apiKey } });
  if (!r.ok) throw new Error(`Upstream ${r.status}`);
  return r.json();
}
