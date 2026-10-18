'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { createHmac } = require('node:crypto');
const { createHandler, validateTicket } = require('../api/blob-upload.js');
const { createUploader } = require('../web/hosted-upload.js');

// Synthetic fixture credentials, never loaded from the runtime environment.
const env = {
  HAX_APP_ORIGIN: 'https://mentor.example',
  HAX_UPLOAD_SIGNING_SECRET: 'test-upload-signing-key-only',
  HAX_INTERNAL_SECRET: 'test-internal-key-only',
  BLOB_READ_WRITE_TOKEN: 'vercel_blob_rw_teststore_testkey',
};
const NOW = 1800000000;
const payload = {
  v: 1, upload_id: 'a'.repeat(32), owner: 'b'.repeat(64),
  workspace_id: 'c'.repeat(32),
  pathname: `raw/${'b'.repeat(64)}/${'c'.repeat(32)}/${'a'.repeat(32)}/source.imscc`,
  size: 8 * 1024 ** 3, kind: 'course', expires_at: NOW + 3600,
};

function sign(fields = {}, secret = env.HAX_UPLOAD_SIGNING_SECRET) {
  const segment = Buffer.from(JSON.stringify({ ...payload, ...fields })).toString('base64url');
  return `${segment}.${createHmac('sha256', secret).update(segment).digest('base64url')}`;
}

function generateBody(ticket = sign(), fields = {}) {
  return {
    type: 'blob.generate-client-token',
    payload: {
      pathname: payload.pathname, multipart: true,
      clientPayload: JSON.stringify({ ticket }), ...fields,
    },
  };
}

function completeBody(ticket = sign(), blobFields = {}) {
  return {
    type: 'blob.upload-completed',
    payload: {
      tokenPayload: JSON.stringify({ ticket }),
      blob: {
        pathname: payload.pathname,
        url: 'https://teststore.private.blob.vercel-storage.com/raw/source.imscc',
        contentType: 'application/octet-stream',
        ...blobFields,
      },
    },
  };
}

const mockSdk = {
  async handleUpload(options) {
    if (options.body.type === 'blob.generate-client-token') {
      return options.onBeforeGenerateToken(
        options.body.payload.pathname,
        options.body.payload.clientPayload,
        options.body.payload.multipart,
      );
    }
    await options.onUploadCompleted(options.body.payload);
    return { type: 'blob.upload-completed', response: 'ok' };
  },
};

async function invoke(body, overrides = {}, requestFields = {}) {
  const handler = createHandler({
    env, now: () => NOW, loadSdk: async () => mockSdk,
    fetchImpl: async () => { throw new Error('Unexpected network request'); },
    ...overrides,
  });
  const response = {
    headers: {},
    setHeader(name, value) { this.headers[name] = value; },
    status(status) { this.statusCode = status; return this; },
    json(value) { this.body = value; return this; },
  };
  await handler({
    method: 'POST', body,
    headers: { host: 'attacker.example', 'x-forwarded-host': 'attacker.example' },
    url: 'https://attacker.example/api/blob-upload?redirect=evil',
    ...requestFields,
  }, response);
  return response;
}

test('valid Python-compatible compact HMAC ticket preserves exact claims', () => {
  assert.deepEqual(validateTicket(sign(), {
    secret: env.HAX_UPLOAD_SIGNING_SECRET, now: NOW, pathname: payload.pathname,
  }), payload);
});

test('rejects forged, malformed and non-matching tickets', () => {
  const options = { secret: env.HAX_UPLOAD_SIGNING_SECRET, now: NOW };
  for (const ticket of [
    sign({}, 'wrong-key'), 'invalid', `${sign()}.extra`, '', null,
    `e30.${'A'.repeat(43)}`, sign({ v: 2 }), sign({ owner: 'no-owner' }),
    sign({ upload_id: 'a'.repeat(31) }), sign({ workspace_id: 'c'.repeat(33) }),
    sign({ owner: ['b'.repeat(64)] }), sign({ owner: 'd'.repeat(64) }),
    sign({ kind: 'unknown' }), sign({ pathname: 'raw/../source.imscc' }),
    sign({ pathname: 'raw/a%2Fb/source.imscc' }), sign({ pathname: 'https://evil/file' }),
  ]) assert.throws(() => validateTicket(ticket, options), /Upload request rejected/);
  assert.throws(() => validateTicket(sign(), { ...options, pathname: 'raw/other' }));
});

test('enforces safe size integers and one-hour expiration bounds', () => {
  const options = { secret: env.HAX_UPLOAD_SIGNING_SECRET, now: NOW };
  for (const size of [0, -1, 1.1, '100', 8 * 1024 ** 3 + 1, Number.MAX_SAFE_INTEGER]) {
    assert.throws(() => validateTicket(sign({ size }), options));
  }
  for (const expires_at of [NOW, NOW - 1, NOW + 3601, 1.5, '1800000000']) {
    assert.throws(() => validateTicket(sign({ expires_at }), options));
  }
  assert.equal(validateTicket(sign({ size: 1 }), options).size, 1);
});

test('callback expiration grace is bounded to 24h and never bypasses signature', () => {
  const options = { secret: env.HAX_UPLOAD_SIGNING_SECRET, now: NOW, allowExpired: true };
  assert.equal(validateTicket(sign({ expires_at: NOW - 3600 }), options).expires_at, NOW - 3600);
  assert.throws(() => validateTicket(sign({ expires_at: NOW - 86400 }), options));
  assert.throws(() => validateTicket(sign({}, 'forged'), options));
});

test('token hook pins exact SDK constraints and callback origin, ignoring Host', async () => {
  const response = await invoke(generateBody());
  assert.equal(response.statusCode, 200);
  assert.deepEqual(response.body, {
    maximumSizeInBytes: payload.size,
    allowedContentTypes: ['application/octet-stream', 'application/zip'],
    addRandomSuffix: false, allowOverwrite: false,
    validUntil: payload.expires_at * 1000,
    tokenPayload: JSON.stringify({ ticket: sign() }),
    callbackUrl: 'https://mentor.example/api/blob-upload',
  });
  assert.equal(response.headers['Cache-Control'], 'no-store');
  const materialPath = payload.pathname.replace(/imscc$/, 'pdf');
  const material = await invoke(generateBody(sign({
    kind: 'material', pathname: materialPath, size: 10 * 1024 ** 2,
  }), { pathname: materialPath }));
  assert.ok(material.body.allowedContentTypes.includes('application/pdf'));
});

test('bad client payload, path, ticket or non-multipart request cannot mint', async () => {
  for (const body of [
    generateBody(sign({}, 'wrong')), generateBody(sign({ expires_at: NOW })),
    generateBody(sign(), { pathname: 'raw/wrong' }),
    generateBody(sign(), { multipart: false }),
    generateBody(sign(), { clientPayload: '{}' }),
    generateBody(sign(), { clientPayload: 'invalid-json' }),
  ]) assert.equal((await invoke(body)).statusCode, 403);
});

test('configuration rejects missing credentials and arbitrary callback URLs', async () => {
  for (const HAX_APP_ORIGIN of [
    undefined, 'http://mentor.example', 'https://mentor.example/',
    'https://mentor.example/path', 'https://mentor.example?x=1',
    'https://user:pass@mentor.example', 'https://mentor.example#evil',
    'https://mentor.example\\@evil.example',
  ]) {
    const response = await invoke(generateBody(), { env: { ...env, HAX_APP_ORIGIN } });
    assert.equal(response.statusCode, 503);
    assert.deepEqual(response.body, { error: 'Upload request rejected' });
  }
  for (const key of ['HAX_UPLOAD_SIGNING_SECRET', 'HAX_INTERNAL_SECRET', 'BLOB_READ_WRITE_TOKEN']) {
    assert.equal((await invoke(generateBody(), { env: { ...env, [key]: '' } })).statusCode, 503);
  }
});

test('verified callback forwards only ticket/blob to pinned backend with auth and timeout', async () => {
  const body = completeBody(sign({ expires_at: NOW - 3600 }));
  let called = 0;
  const response = await invoke(body, {
    fetchImpl: async (url, options) => {
      called++;
      assert.equal(url, 'https://mentor.example/mentor/internal/upload-complete');
      assert.equal(options.headers.Authorization, `Bearer ${env.HAX_INTERNAL_SECRET}`);
      assert.equal(options.method, 'POST');
      assert.equal(options.redirect, 'error');
      assert.ok(options.signal instanceof AbortSignal);
      assert.deepEqual(JSON.parse(options.body), {
        ticket: JSON.parse(body.payload.tokenPayload).ticket, blob: body.payload.blob,
      });
      return { ok: true };
    },
  });
  assert.equal(called, 1);
  assert.equal(response.statusCode, 200);
});

test('invalid callback claims and blob shape fail before backend forwarding', async () => {
  for (const body of [
    completeBody(sign({}, 'wrong')),
    completeBody(sign({ expires_at: NOW - 86400 })),
    completeBody(sign(), { pathname: 'raw/wrong' }),
    completeBody(sign(), { url: 'http://evil.example' }),
    completeBody(sign(), { contentType: null }),
    { ...completeBody(), payload: { tokenPayload: '{}' } },
  ]) assert.equal((await invoke(body)).statusCode, 403);
});

test('backend failures are retryable and errors never disclose credentials', async () => {
  for (const fetchImpl of [
    async () => ({ ok: false }),
    async () => { throw new Error(Object.values(env).join(' ')); },
  ]) {
    const response = await invoke(completeBody(), { fetchImpl });
    assert.equal(response.statusCode, 503);
    assert.deepEqual(response.body, { error: 'Upload request rejected' });
  }
  const response = await invoke(generateBody(), {
    loadSdk: async () => { throw new Error(Object.values(env).join(' ')); },
  });
  assert.deepEqual(response.body, { error: 'Upload request rejected' });
});

test('handler handles JSON bodies and rejects malformed/oversized bodies or other methods', async () => {
  assert.equal((await invoke(JSON.stringify(generateBody()))).statusCode, 200);
  assert.equal((await invoke(Buffer.from(JSON.stringify(generateBody())))).statusCode, 200);
  for (const body of ['{', 'x'.repeat(65537), null, [], {}, { type: 'invalid', payload: {} }]) {
    assert.equal((await invoke(body)).statusCode, 403);
  }
  const response = await invoke(generateBody(), {}, { method: 'GET' });
  assert.equal(response.statusCode, 405);
  assert.equal(response.headers.Allow, 'POST');
});

test('browser passes original large File without buffering and official private multipart options', async () => {
  const file = {
    size: payload.size, type: '',
    arrayBuffer() { throw new Error('Must not buffer large files'); },
  };
  const intent = {
    upload_id: payload.upload_id, pathname: payload.pathname,
    ticket: sign(), maximum_size: payload.size,
  };
  const onProgress = () => {};
  const signal = new AbortController().signal;
  const receipt = { pathname: payload.pathname };
  const upload = createUploader(async (pathname, body, options) => {
    assert.equal(pathname, intent.pathname);
    assert.equal(body, file);
    assert.deepEqual(options, {
      access: 'private', handleUploadUrl: '/api/blob-upload', multipart: true,
      contentType: 'application/octet-stream',
      clientPayload: JSON.stringify({ ticket: intent.ticket }),
      onUploadProgress: onProgress, abortSignal: signal,
    });
    assert.equal('token' in options, false);
    return receipt;
  });
  assert.equal(await upload(file, intent, { onProgress, signal }), receipt);
  await assert.rejects(upload({ size: payload.size + 1 }, intent));
  await assert.rejects(upload(file, { ...intent, maximum_size: 0 }));
});

test('raw Node request streams are accepted but bounded before SDK execution', async () => {
  const { Readable } = require('node:stream');
  const stream = Readable.from([Buffer.from(JSON.stringify(generateBody()))]);
  stream.method = 'POST';
  const response = {
    setHeader() {},
    status(code) { this.statusCode = code; return this; },
    json(body) { this.body = body; return this; },
  };
  const handler = createHandler({ env, now: () => NOW, loadSdk: async () => mockSdk });
  await handler(stream, response);
  assert.equal(response.statusCode, 200);
  const oversized = Readable.from([Buffer.alloc(32768), Buffer.alloc(32769)]);
  oversized.method = 'POST';
  await handler(oversized, response);
  assert.equal(response.statusCode, 403);
});

test('kind-specific sizes and source extensions match the backend admission policy', () => {
  const options = { secret: env.HAX_UPLOAD_SIGNING_SECRET, now: NOW };
  for (const fields of [
    { kind: 'material', pathname: payload.pathname.replace(/imscc$/, 'pdf'), size: 10 * 1024 ** 2 + 1 },
    { kind: 'course', pathname: payload.pathname.replace(/imscc$/, 'haxcourse'), size: 64 * 1024 ** 2 + 1 },
    { kind: 'material', pathname: payload.pathname.replace(/imscc$/, 'htm'), size: 10 },
    { kind: 'material', pathname: payload.pathname.replace(/imscc$/, 'markdown'), size: 10 },
    { kind: 'course', pathname: payload.pathname.replace(/imscc$/, 'zip'), size: 10 },
  ]) assert.throws(() => validateTicket(sign(fields), options));
  assert.equal(validateTicket(sign({
    pathname: payload.pathname.replace(/imscc$/, 'haxcourse'), size: 64 * 1024 ** 2,
  }), options).size, 64 * 1024 ** 2);
});

let publishedSdk;
try {
  publishedSdk = require(process.env.HAX_BLOB_SDK_MODULE || '@vercel/blob/client');
} catch {
  // Core tests use injected SDK/fetch mocks; this extra contract suite is opt-in
  // when dependencies are installed or an inspection module path is supplied.
}

test('published SDK generates scoped tokens offline and verifies callback signatures', {
  skip: !publishedSdk,
}, async () => {
  const sdkOverrides = { loadSdk: async () => publishedSdk };
  const minted = await invoke(generateBody(), sdkOverrides);
  assert.equal(minted.statusCode, 200);
  const claims = publishedSdk.getPayloadFromClientToken(minted.body.clientToken);
  assert.equal(claims.pathname, payload.pathname);
  assert.equal(claims.maximumSizeInBytes, payload.size);
  assert.equal(claims.allowOverwrite, false);
  assert.equal(claims.addRandomSuffix, false);
  assert.equal(claims.onUploadCompleted.callbackUrl, 'https://mentor.example/api/blob-upload');
  const body = completeBody();
  let forwarded = 0;
  const overrides = {
    ...sdkOverrides,
    fetchImpl: async () => { forwarded++; return { ok: true }; },
  };
  for (const signature of ['', '00'.repeat(32)]) {
    assert.equal((await invoke(body, overrides, {
      headers: { 'x-vercel-signature': signature },
    })).statusCode, 403);
  }
  assert.equal(forwarded, 0);
  const signature = createHmac('sha256', env.BLOB_READ_WRITE_TOKEN)
    .update(JSON.stringify(body)).digest('hex');
  assert.equal((await invoke(body, overrides, {
    headers: { 'x-vercel-signature': signature },
  })).statusCode, 200);
  assert.equal(forwarded, 1);
});
