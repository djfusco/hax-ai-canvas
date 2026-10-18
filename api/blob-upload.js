'use strict';

const { createHmac, timingSafeEqual } = require('node:crypto');

const MAX_SIZE = 8 * 1024 ** 3;
const MAX_BODY = 64 * 1024;
const CALLBACK_GRACE_SECONDS = 24 * 3600;
const COURSE_TYPES = ['application/octet-stream', 'application/zip'];
const MATERIAL_TYPES = [
  ...COURSE_TYPES, 'application/pdf', 'text/plain', 'text/markdown',
  'text/csv', 'text/html',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation',
];

class UploadError extends Error {
  constructor(status = 403) {
    super('Upload request rejected');
    this.status = status;
  }
}

function matchesIntentPath(payload) {
  const prefix = `raw/${payload.owner}/${payload.workspace_id}/${payload.upload_id}/source.`;
  if (!payload.pathname.startsWith(prefix)) return false;
  const extension = payload.pathname.slice(prefix.length);
  return payload.kind === 'course'
    ? ['imscc', 'haxcourse'].includes(extension)
    : ['txt', 'md', 'csv', 'html', 'pdf', 'docx', 'pptx'].includes(extension);
}

function object(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function validPathname(value) {
  return typeof value === 'string' && value.length <= 1024 &&
    /^raw\/[A-Za-z0-9_./-]+$/.test(value) &&
    value.split('/').every((part) => part && part !== '.' && part !== '..');
}

// The signed segment is authenticated as received, not parsed and reserialized.
function validateTicket(ticket, {
  secret, pathname, now = Math.floor(Date.now() / 1000), allowExpired = false,
} = {}) {
  if (typeof secret !== 'string' || !secret) throw new UploadError(503);
  if (typeof ticket !== 'string' || ticket.length > 8192) throw new UploadError();
  const parts = ticket.split('.');
  if (parts.length !== 2 || !parts.every((part) => /^[A-Za-z0-9_-]+$/.test(part))) {
    throw new UploadError();
  }
  const signature = Buffer.from(parts[1], 'base64url');
  const expected = createHmac('sha256', secret).update(parts[0]).digest();
  if (signature.length !== expected.length || !timingSafeEqual(signature, expected)) {
    throw new UploadError();
  }
  let payload;
  try {
    payload = JSON.parse(Buffer.from(parts[0], 'base64url').toString('utf8'));
  } catch {
    throw new UploadError();
  }
  if (!object(payload) || payload.v !== 1 ||
      typeof payload.upload_id !== 'string' ||
      !/^[a-f0-9]{32}$/.test(payload.upload_id) ||
      typeof payload.owner !== 'string' ||
      !/^[a-f0-9]{64}$/.test(payload.owner) ||
      typeof payload.workspace_id !== 'string' ||
      !/^[a-f0-9]{32}$/.test(payload.workspace_id) ||
      !validPathname(payload.pathname) ||
      (pathname !== undefined && pathname !== payload.pathname) ||
      !Number.isSafeInteger(payload.size) || payload.size < 1 || payload.size > MAX_SIZE ||
      !['course', 'material'].includes(payload.kind) ||
      !matchesIntentPath(payload) ||
      (payload.kind === 'material' && payload.size > 10 * 1024 ** 2) ||
      (payload.pathname.endsWith('.haxcourse') && payload.size > 64 * 1024 ** 2) ||
      !Number.isSafeInteger(payload.expires_at) ||
      payload.expires_at > now + 3600 ||
      payload.expires_at <= now - (allowExpired ? CALLBACK_GRACE_SECONDS : 0)) {
    throw new UploadError();
  }
  return payload;
}

function configuredOrigin(value) {
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:' || url.origin !== value ||
        url.username || url.password) throw new Error();
    return url.origin;
  } catch {
    throw new UploadError(503);
  }
}

function ticketFromPayload(value) {
  if (typeof value !== 'string' || value.length > 16384) throw new UploadError();
  try {
    const parsed = JSON.parse(value);
    if (!object(parsed) || typeof parsed.ticket !== 'string') throw new Error();
    return parsed.ticket;
  } catch {
    throw new UploadError();
  }
}

async function requestBody(request) {
  let body = request.body;
  if (body === undefined) {
    const chunks = [];
    let size = 0;
    for await (const chunk of request) {
      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      size += bytes.length;
      if (size > MAX_BODY) throw new UploadError();
      chunks.push(bytes);
    }
    body = Buffer.concat(chunks);
  }
  if (Buffer.isBuffer(body)) body = body.toString('utf8');
  if (typeof body === 'string') {
    if (Buffer.byteLength(body) > MAX_BODY) throw new UploadError();
    try { body = JSON.parse(body); } catch { throw new UploadError(); }
  }
  if (!object(body) || Buffer.byteLength(JSON.stringify(body)) > MAX_BODY ||
      !object(body.payload) ||
      !['blob.generate-client-token', 'blob.upload-completed'].includes(body.type)) {
    throw new UploadError();
  }
  return body;
}

function validateBlob(blob, pathname) {
  if (!object(blob) || blob.pathname !== pathname ||
      typeof blob.contentType !== 'string' || blob.contentType.length > 256) {
    throw new UploadError();
  }
  try {
    const url = new URL(blob.url);
    if (url.protocol !== 'https:' || url.username || url.password) throw new Error();
  } catch {
    throw new UploadError();
  }
}

function createHandler({
  env = process.env,
  loadSdk = () => import('@vercel/blob/client'),
  fetchImpl = (...args) => fetch(...args),
  now = () => Math.floor(Date.now() / 1000),
} = {}) {
  return async function blobUpload(request, response) {
    response.setHeader('Cache-Control', 'no-store');
    if (request.method !== 'POST') {
      response.setHeader('Allow', 'POST');
      return response.status(405).json({ error: 'Upload request rejected' });
    }
    try {
      const origin = configuredOrigin(env.HAX_APP_ORIGIN);
      if (!env.HAX_UPLOAD_SIGNING_SECRET || !env.HAX_INTERNAL_SECRET ||
          !env.BLOB_READ_WRITE_TOKEN) throw new UploadError(503);
      const body = await requestBody(request);
      const { handleUpload } = await loadSdk();
      // The official SDK authenticates upload-completed signatures before this hook.
      const result = await handleUpload({
        request, body, token: env.BLOB_READ_WRITE_TOKEN,
        onBeforeGenerateToken: async (pathname, clientPayload, multipart) => {
          if (multipart !== true) throw new UploadError();
          const ticket = ticketFromPayload(clientPayload);
          const intent = validateTicket(ticket, {
            secret: env.HAX_UPLOAD_SIGNING_SECRET, pathname, now: now(),
          });
          return {
            maximumSizeInBytes: intent.size,
            allowedContentTypes: intent.kind === 'course' ? COURSE_TYPES : MATERIAL_TYPES,
            addRandomSuffix: false,
            allowOverwrite: false,
            validUntil: intent.expires_at * 1000,
            tokenPayload: JSON.stringify({ ticket }),
            callbackUrl: `${origin}/api/blob-upload`,
          };
        },
        onUploadCompleted: async ({ blob, tokenPayload }) => {
          const ticket = ticketFromPayload(tokenPayload);
          const intent = validateTicket(ticket, {
            secret: env.HAX_UPLOAD_SIGNING_SECRET, now: now(), allowExpired: true,
          });
          validateBlob(blob, intent.pathname);
          // Python rechecks the persisted intent and heads the private blob itself.
          let completed;
          try {
            completed = await fetchImpl(`${origin}/mentor/internal/upload-complete`, {
              method: 'POST',
              headers: {
                'Content-Type': 'application/json',
                Authorization: `Bearer ${env.HAX_INTERNAL_SECRET}`,
              },
              body: JSON.stringify({ ticket, blob }),
              redirect: 'error',
              signal: AbortSignal.timeout(10000),
            });
          } catch {
            throw new UploadError(503);
          }
          if (!completed.ok) throw new UploadError(503);
        },
      });
      return response.status(200).json(result);
    } catch (error) {
      // SDK and downstream exceptions can contain credentials or signed payloads.
      return response.status(error instanceof UploadError ? error.status : 403)
        .json({ error: 'Upload request rejected' });
    }
  };
}

module.exports = createHandler();
module.exports.createHandler = createHandler;
module.exports.validateTicket = validateTicket;
