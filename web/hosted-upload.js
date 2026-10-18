'use strict';

// Bundle this entry for the browser; no store credential is ever exposed here.
function createUploader(sdkUpload) {
  return async function upload(file, intent, { onProgress, signal } = {}) {
    if (!file || !Number.isSafeInteger(file.size) || file.size < 1 ||
        !intent || !/^[a-f0-9]{32}$/.test(intent.upload_id) ||
        typeof intent.pathname !== 'string' || !intent.pathname.startsWith('raw/') ||
        typeof intent.ticket !== 'string' || !intent.ticket ||
        !Number.isSafeInteger(intent.maximum_size) ||
        intent.maximum_size < 1 || intent.maximum_size > 8 * 1024 ** 3 ||
        file.size > intent.maximum_size) {
      throw new Error('Invalid upload intent or file size');
    }
    // Explicit MIME avoids empty/incorrect browser MIME inference for .imscc files.
    // Passing the File/Blob unchanged keeps multipart reads bounded inside the SDK.
    return sdkUpload(intent.pathname, file, {
      access: 'private',
      handleUploadUrl: '/api/blob-upload',
      multipart: true,
      contentType: 'application/octet-stream',
      clientPayload: JSON.stringify({ ticket: intent.ticket }),
      onUploadProgress: onProgress,
      abortSignal: signal,
    });
  };
}

const upload = createUploader((...args) => require('@vercel/blob/client').upload(...args));

// The authenticated caller must POST /mentor/api/uploads/<upload_id>/complete
// with CSRF after success, regardless of callback timing. It must not trust the
// returned browser receipt as proof that a blob is valid or belongs to its owner.
if (typeof window !== 'undefined') window.HaxHostedUpload = { upload };

module.exports = { upload, createUploader };
