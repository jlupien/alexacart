#!/usr/bin/env node

/**
 * Cookie refresh sidecar for AlexaCart.
 *
 * Reads existing cookie data from stdin (JSON),
 * uses alexa-cookie2 to refresh the cookies,
 * writes new cookie data to stdout (JSON).
 *
 * Called from Python via subprocess.
 */

const alexaCookie = require('alexa-cookie2');

let input = '';

process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => { input += chunk; });

process.stdin.on('end', async () => {
  try {
    const existing = JSON.parse(input);

    if (!existing.cookies || !existing.registration) {
      process.stderr.write('No registration data found. Manual login required.\n');
      process.exit(1);
    }

    const options = {
      formerRegistrationData: existing.registration,
    };

    alexaCookie.refreshAlexaCookie(options, (err, result) => {
      if (err) {
        process.stderr.write(`Refresh failed: ${err.message}\n`);
        process.exit(1);
      }

      const output = {
        cookies: result.cookie ? parseCookieString(result.cookie) : existing.cookies,
        registration: result.formerRegistrationData || existing.registration,
        source: 'sidecar_refresh',
      };

      process.stdout.write(JSON.stringify(output, null, 2));
      process.exit(0);
    });
  } catch (e) {
    process.stderr.write(`Error: ${e.message}\n`);
    process.exit(1);
  }
});

function parseCookieString(cookieStr) {
  const cookies = {};
  cookieStr.split(';').forEach((pair) => {
    const [key, ...vals] = pair.trim().split('=');
    if (key) {
      cookies[key.trim()] = vals.join('=').trim();
    }
  });
  return cookies;
}
