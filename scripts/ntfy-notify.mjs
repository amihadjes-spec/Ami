#!/usr/bin/env node
// Send a push notification via ntfy (https://ntfy.sh or a self-hosted instance).
//
// Usage:
//   node scripts/ntfy-notify.mjs "message text"
//
// Config (env vars):
//   NTFY_TOPIC    required — the ntfy topic to publish to
//   NTFY_SERVER   optional — base URL of the ntfy server (default: https://ntfy.sh)
//   NTFY_TITLE    optional — notification title
//   NTFY_PRIORITY optional — 1 (min) .. 5 (max)
//   NTFY_TAGS     optional — comma-separated emoji short-codes/tags

const message = process.argv[2];

if (!message) {
  console.error('Usage: node scripts/ntfy-notify.mjs "<message>"');
  process.exit(1);
}

const topic = process.env.NTFY_TOPIC;
if (!topic) {
  console.error('Missing required env var NTFY_TOPIC.');
  process.exit(1);
}

const server = (process.env.NTFY_SERVER || 'https://ntfy.sh').replace(/\/+$/, '');
const url = `${server}/${topic}`;

const headers = { 'Content-Type': 'text/plain; charset=utf-8' };
if (process.env.NTFY_TITLE) headers['Title'] = process.env.NTFY_TITLE;
if (process.env.NTFY_PRIORITY) headers['Priority'] = process.env.NTFY_PRIORITY;
if (process.env.NTFY_TAGS) headers['Tags'] = process.env.NTFY_TAGS;

try {
  const response = await fetch(url, {
    method: 'POST',
    headers,
    body: message,
  });

  if (!response.ok) {
    const body = await response.text().catch(() => '');
    console.error(`ntfy request failed: ${response.status} ${response.statusText}${body ? ` — ${body}` : ''}`);
    process.exit(1);
  }

  console.log(`Notification sent to ${url}`);
} catch (err) {
  console.error(`ntfy request errored: ${err.message}`);
  process.exit(1);
}
